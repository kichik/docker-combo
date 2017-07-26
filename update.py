#!/usr/bin/python

import argparse
import io
import json
import logging
import os
import sys
import time

import bs4
import docker
import docker.errors
import requests

docker_client = docker.from_env()


def parse_cmdline():
    def check_docker_tag(value):
        if value.count(':') != 1 or value[0] == ':' or value[-1] == ':':
            raise argparse.ArgumentTypeError('%s is an invalid Docker tag' % value)
        return value

    parser = argparse.ArgumentParser()
    parser.add_argument('--push', action='store_true')
    parser.add_argument('images', metavar='IMAGE', type=check_docker_tag, nargs=2)
    return parser.parse_args()


class DockerImageError(Exception):
    pass


class DockerImage(object):
    def __init__(self, image):
        self.image = image
        self._build_time = None
        self._dockerfile = None

    @property
    def user(self):
        if '/' in self.image:
            return self.image.split('/')[0]
        return '_'

    @property
    def repo(self):
        return self.image.split(':')[0].split('/')[-1]

    @property
    def tag(self):
        return self.image.split(':')[1]

    @property
    def build_time(self):
        if self._build_time:
            return self._build_time

        # TODO get details without pulling
        logging.info('Pulling %s', self.image)

        try:
            docker_client.pull(self.image)
        except docker.errors.NotFound:
            raise DockerImageError('%s not found', self.image)

        for i in docker_client.images():
            if self.image in i['RepoTags']:
                self._build_time = i['Created']
                built_time_str = time.ctime(self._build_time)
                logging.info('%s was last built on %s (%d)', self.image, built_time_str, self._build_time)
                return self._build_time

        raise DockerImageError('Error pulling %s', self.image)

    @property
    def dockerfile(self):
        if self._dockerfile:
            return self._dockerfile

        # TODO there must be a better way...
        url = 'https://hub.docker.com/%s/%s/' % (self.user, self.repo)
        hub_req = requests.get(url)
        if hub_req.status_code != 200:
            raise DockerImageError('Error connecting to hub (%s)' % hub_req.text)

        soup = bs4.BeautifulSoup(hub_req.text, 'html.parser')
        for node in soup(text=self.tag):
            if node.parent.name == 'code' and node.parent.parent.name == 'a':
                dockerfile_url = node.parent.parent.get('href') \
                    .replace('github.com', 'raw.githubusercontent.com').replace('/blob/', '/')
                docekrfile_req = requests.get(dockerfile_url)
                if docekrfile_req.status_code != 200:
                    raise DockerImageError('Error downloading Dockerfile (%s)' % dockerfile_url)

                self._dockerfile = docekrfile_req.text
                return docekrfile_req.text

        raise DockerImageError('Unable to find Dockerfile in %s' % url)


def should_rebuild(combo_image, image1, image2):
    try:
        combo_image_time = combo_image.build_time
    except DockerImageError:
        combo_image_time = 0

    image1_time = image1.build_time
    image2_time = image2.build_time

    return combo_image_time < image1_time or combo_image_time < image2_time


def combine_image_name_and_tag(image1, image2):
    name = image1.split(':')[0] + '_' + image2.split(':')[0]
    tag = image1.split(':')[1] + '_' + image2.split(':')[1]
    return 'combos/%s:%s' % (name, tag)


def combine_dockerfiles(dockerfile1, dockerfile2):
    lines1 = dockerfile1.splitlines()
    lines2 = dockerfile2.splitlines()

    return '\n'.join(l for l in lines1 + lines2[1:] if not l.startswith('CMD ') or l.startswith('ENTRYPOINT '))


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)-15s %(levelname)s %(message)s')
    args = parse_cmdline()
    image1 = DockerImage(args.images[0])
    image2 = DockerImage(args.images[1])

    if image1.dockerfile.splitlines()[0] != image2.dockerfile.splitlines()[0]:
        logging.error('%s and %s do not use the same FROM line', image1.image, image2.image)
        return 1

    combo_image = DockerImage(combine_image_name_and_tag(image1.image, image2.image))
    if not should_rebuild(combo_image, image1, image2):
        logging.info('Up-to-date')
        return 0

    logging.info('Rebuilding...')

    build_stream = docker_client.build(
        fileobj=io.BytesIO(combine_dockerfiles(image1.dockerfile, image2.dockerfile).encode('utf-8')),
        tag=combo_image.image
    )

    for line in build_stream:
        logging.info('%s', json.loads(line.decode('utf-8'))['stream'].strip('\n'))

    if args.push:
        docker_client.login(os.getenv('DOCKER_USERNAME'), os.getenv('DOCKER_PASSWORD'))
        docker_client.push('%s/%s' % (combo_image.user, combo_image.repo), combo_image.tag)

    return 0


if __name__ == '__main__':
    sys.exit(main())
