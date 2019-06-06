#!/usr/bin/python

import argparse
import io
import json
import logging
import os
import re
import sys
import tarfile
import time

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


class BuildContext(object):
    def __init__(self):
        self.context_bytes = io.BytesIO()
        self.context = tarfile.TarFile(fileobj=self.context_bytes, mode='w')
        self.file_id = 0
        self.dockerfile = ''
        self.first = True
        self.saw_from = False

    def add_image(self, image):
        for line in image.dockerfile.splitlines():
            line = line.strip()
            if line.upper().startswith('FROM '):
                if self.first:
                    if self.saw_from:
                        raise DockerBuildError('multi-stage not supported yet')
                    self.dockerfile += line + '\n'
                    self.saw_from = True

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

                self.dockerfile += 'COPY %s %s\n' % (self._extract_file_from_image(image.image, path), path)

            elif line.upper().startswith('CMD ') or line.upper().startswith('ENTRYPOINT '):
                continue

            else:
                self.dockerfile += line + '\n'

        self.first = False

    def finalize(self):
        dockerfile_content = self.dockerfile.encode('utf-8')

        info = tarfile.TarInfo('Dockerfile')
        info.size = len(dockerfile_content)
        self.context.addfile(info, io.BytesIO(dockerfile_content))

        self.context.close()
        self.context_bytes.seek(0)

        return self.context_bytes

    def _next_file_id(self):
        self.file_id += 1
        return 'extracted' + str(self.file_id)

    def _extract_file_from_image(self, image, path):
        logging.info('Extracting %s from %s...', path, image)

        container = docker_hl_client.containers.create(image)
        try:
            bits, stats = container.get_archive(path)
            buf = io.BytesIO()
            for chunk in bits:
                buf.write(chunk)
            buf.seek(0)
            tar = tarfile.TarFile(fileobj=buf)

            src_info = tar.getmembers()[0]
            src = tar.extractfile(src_info)

            src_info.name = self._next_file_id()
            self.context.addfile(src_info, src)

            return src_info.name
        finally:
            container.stop()
            container.remove()


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

    logging.info('Creating build context...')

    context_builder = BuildContext()
    context_builder.add_image(image1)
    context_builder.add_image(image2)
    context = context_builder.finalize()

    logging.info('Rebuilding...')

    build_stream = docker_client.build(fileobj=context, custom_context=True, tag=combo_image.image)
    log_stream(build_stream)

    if args.push:
        docker_client.login(os.getenv('DOCKER_USERNAME'), os.getenv('DOCKER_PASSWORD'))
        push_stream = docker_client.push('%s/%s' % (combo_image.user, combo_image.repo), combo_image.tag, stream=True)
        log_stream(push_stream)

    return 0


if __name__ == '__main__':
    sys.exit(main())
