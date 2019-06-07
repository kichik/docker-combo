#!/usr/bin/python

import argparse
import io
import json
import logging
import os
import re
import sys

import bs4
import docker
import docker.errors
import markdown
import requests

docker_hl_client = docker.from_env()
docker_client = docker_hl_client.api


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


class DockerBuildError(Exception):
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
            image = docker_hl_client.images.pull(self.image)
        except docker.errors.NotFound:
            raise DockerImageError('%s not found', self.image)

        self._build_time = image.attrs['Created']
        logging.info('%s was last built on %s', self.image, self._build_time)
        return self._build_time

    @property
    def dockerfile(self):
        if self._dockerfile:
            return self._dockerfile

        # TODO there must be a better way...
        if self.user != '_':
            raise DockerImageError('Unable to pull Dockerfile from non-product image on new hub')

        url = 'https://hub.docker.com/api/content/v1/products/images/%s' % (self.repo,)
        hub_req = requests.get(url)
        if hub_req.status_code != 200:
            raise DockerImageError('Error connecting to hub (%s)' % hub_req.text)

        description = hub_req.json().get('full_description', '')
        description_html = markdown.markdown(description)
        soup = bs4.BeautifulSoup(description_html, 'html.parser')
        for node in soup(text=self.tag):
            dockerfile_url = None

            if node.parent.name == 'code' and node.parent.parent.name == 'a':
                dockerfile_url = node.parent.parent.get('href')

            if node.parent.name == 'code' and node.parent.parent.name == 'li':
                dockerfile_urls = [a.get('href') for a in node.parent.parent.find_all('a')]
                dockerfile_urls = [u for u in dockerfile_urls if 'windowsservercore' not in u]
                if len(dockerfile_urls) == 1:
                    dockerfile_url = dockerfile_urls[0]

            if dockerfile_url:
                dockerfile_url = dockerfile_url.replace('github.com', 'raw.githubusercontent.com').replace('/blob/', '/')
                docekrfile_req = requests.get(dockerfile_url)
                if docekrfile_req.status_code != 200:
                    raise DockerImageError('Error downloading Dockerfile (%s)' % dockerfile_url)

                self._dockerfile = docekrfile_req.text
                return docekrfile_req.text

        raise DockerImageError('Unable to find Dockerfile for %s in %s' % (self.tag, url))


def get_from_line(dockerfile):
    for line in dockerfile.splitlines():
        if line.strip().startswith('FROM'):
            return line


def is_compatible_from_lines(dockerfile1, dockerfile2):
    from_line1 = get_from_line(dockerfile1)
    from_line2 = get_from_line(dockerfile2)
    if from_line1 == from_line2:
        return True

    base1 = from_line1.split(' ')[-1].split(':')[0]
    base2 = from_line2.split(' ')[-1].split(':')[0]
    if base1 == base2 == 'buildpack-deps':
        logging.info('Both using FROM buildpack-deps (%s, %s) which are different versions but still compatible',
                     from_line1, from_line2)
        return True

    logging.info('%s != %s', from_line1, from_line2)
    return False


def should_rebuild(combo_image, image1, image2):
    try:
        combo_image_time = combo_image.build_time
    except DockerImageError:
        logging.info('Combo image not built yet')
        combo_image_time = ''

    image1_time = image1.build_time
    image2_time = image2.build_time

    return combo_image_time < image1_time or combo_image_time < image2_time


def combine_image_name_and_tag(image1, image2):
    name = image1.split(':')[0] + '_' + image2.split(':')[0]
    tag = image1.split(':')[1] + '_' + image2.split(':')[1]
    return 'combos/%s:%s' % (name, tag)


def log_stream(stream):
    for lines in stream:
        for line in lines.decode('utf-8').strip().split('\n'):
            line = json.loads(line)
            if line.get('errorDetail'):
                raise DockerBuildError(line['errorDetail'].get('message', str(line)))
            logging.info('%s', line.get('stream', str(line)).strip('\n'))


class DockerfileBuilder(object):
    def __init__(self):
        self.dockerfile = ''
        self.first = True

    def add_image(self, image):
        saw_from = False

        for line in image.dockerfile.splitlines():
            line = line.strip()
            if line.upper().startswith('FROM '):
                if self.first:
                    self.dockerfile += line + '\n'

                if saw_from:
                    raise DockerBuildError('multi-stage not supported yet')
                saw_from = True

            elif line.upper().startswith('COPY'):
                if line.endswith('\\'):
                    raise DockerBuildError('multi-line COPY commands not supported yet')

                m = re.match('^COPY[ \t]+([^ \t]+)[ \t]+([^ \t]+)$', line, re.I)
                if not m:
                    raise DockerBuildError('unable to parse COPY line: ' + line)

                copy_from, copy_to = m.groups()
                if copy_to.endswith('/'):
                    path = copy_to + os.path.basename(copy_from)
                else:
                    path = copy_to

                self.dockerfile += 'COPY --from=%s %s %s\n' % (image.image, path, path)

            elif line.upper().startswith('CMD ') or line.upper().startswith('ENTRYPOINT '):
                continue

            else:
                self.dockerfile += line + '\n'

        self.first = False

    @property
    def file(self):
        return io.BytesIO(self.dockerfile.encode('utf-8'))


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)-15s %(levelname)s %(message)s')
    args = parse_cmdline()
    image1 = DockerImage(args.images[0])
    image2 = DockerImage(args.images[1])

    if not is_compatible_from_lines(image1.dockerfile, image2.dockerfile):
        logging.error('%s and %s do not use the same FROM line', image1.image, image2.image)
        return 1

    combo_image = DockerImage(combine_image_name_and_tag(image1.image, image2.image))
    if not should_rebuild(combo_image, image1, image2):
        logging.info('Up-to-date')
        return 0

    logging.info('Generating Dockerfile...')

    dockerfile = DockerfileBuilder()
    dockerfile.add_image(image1)
    dockerfile.add_image(image2)

    logging.info('Rebuilding...')

    build_stream = docker_client.build(fileobj=dockerfile.file, tag=combo_image.image)
    log_stream(build_stream)

    if args.push:
        docker_client.login(os.getenv('DOCKER_USERNAME'), os.getenv('DOCKER_PASSWORD'))
        push_stream = docker_client.push('%s/%s' % (combo_image.user, combo_image.repo), combo_image.tag, stream=True)
        log_stream(push_stream)

    return 0


if __name__ == '__main__':
    sys.exit(main())
