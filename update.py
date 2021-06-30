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
        image = value.split('@')[0]
        if image.count(':') != 1 or image[0] == ':' or image[-1] == ':':
            raise argparse.ArgumentTypeError('%s is an invalid Docker tag' % value)
        return value

    parser = argparse.ArgumentParser()
    parser.add_argument('--push', action='store_true')
    parser.add_argument('--override-env', action='append', default=[])
    parser.add_argument('--override-from')
    parser.add_argument('--add-gnupg-curl', action='store_true')
    parser.add_argument('images', metavar='IMAGE', type=check_docker_tag, nargs='+')
    return parser.parse_args()


class DockerImageError(Exception):
    pass


class DockerBuildError(Exception):
    pass


class DockerImage(object):
    def __init__(self, image):
        if '@' in image:
            self.image, dockerfile_url = image.split('@')
            docekrfile_req = requests.get(dockerfile_url)
            if docekrfile_req.status_code != 200:
                raise DockerImageError('Error downloading Dockerfile (%s)' % dockerfile_url)
            self._dockerfile = docekrfile_req.text
        else:
            self.image = image
            self._dockerfile = None
        self._build_time = None

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
                dockerfile_url = dockerfile_url.replace('github.com', 'raw.githubusercontent.com').replace('/blob/',
                                                                                                           '/')
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


def is_compatible_from_lines(images):
    from_lines = [get_from_line(i.dockerfile) for i in images]
    if all(from_lines[0] == f for f in from_lines):
        return True

    bases = [f.split(' ')[-1].split(':')[0] for f in from_lines]

    if all(b == 'buildpack-deps' for b in bases):
        logging.info('Images using FROM buildpack-deps (%s) which are different versions but still compatible',
                     ', '.join(from_lines))
        return True

    logging.info('%s', from_lines)
    return False


def should_rebuild(combo_image, base_images):
    try:
        combo_image_time = combo_image.build_time
    except DockerImageError:
        logging.info('Combo image not built yet')
        combo_image_time = ''

    times = [i.build_time for i in base_images]

    return any(combo_image_time < t for t in times)


def combine_image_name_and_tag(images):
    name = '_'.join(i.image.split(':')[0] for i in images)
    tag = '_'.join(i.image.split(':')[1] for i in images)
    return f'combos/{name}:{tag}'


def log_stream(stream):
    for lines in stream:
        for line in lines.decode('utf-8').strip().split('\n'):
            line = json.loads(line)
            if line.get('errorDetail'):
                raise DockerBuildError(line['errorDetail'].get('message', str(line)))
            logging.info('%s', line.get('stream', str(line)).strip('\n'))


class DockerfileBuilder(object):
    def __init__(self, from_override, env_overrides):
        self.dockerfile = ''
        self._use_from = True
        self._env_overrides = env_overrides

        if from_override:
            self._use_from = False
            self.dockerfile += f'FROM {from_override}\n'

    def add_image(self, image):
        saw_from = False

        for line in image.dockerfile.splitlines():
            line = line.strip()
            if line.upper().startswith('FROM '):
                if self._use_from:
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

            elif line.upper().startswith('ENV '):
                _, name, value = re.split('[ \t]+', line, 2)
                if name in self._env_overrides:
                    self.dockerfile += f'ENV {name} {self._env_overrides[name]}\n'
                else:
                    self.dockerfile += line + '\n'

            else:
                self.dockerfile += line + '\n'

        self._use_from = False

    @property
    def file(self):
        return io.BytesIO(self.dockerfile.encode('utf-8'))


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)-15s %(levelname)s %(message)s')
    args = parse_cmdline()
    images = [DockerImage(i) for i in args.images]
    override_env = dict(i.split('=', 1) for i in args.override_env)

    if not args.override_from:
        if not is_compatible_from_lines(images):
            logging.error('%s do not use the same FROM line', ' and '.join(i.image for i in images))
            return 1

    combo_image = DockerImage(combine_image_name_and_tag(images))
    if not should_rebuild(combo_image, images):
        logging.info('Up-to-date')
        return 0

    logging.info('Generating Dockerfile...')

    dockerfile = DockerfileBuilder(args.override_from, override_env)
    if args.add_gnupg_curl:
        dockerfile.dockerfile += 'RUN apt-get update && ' \
                                 'apt-get install -y --no-install-recommends gnupg-curl && ' \
                                 'rm -rf /var/lib/apt/lists/*\n'

    for i in images:
        dockerfile.add_image(i)

    # sks servers are deprecated https://sks-keyservers.net/
    dockerfile.dockerfile = dockerfile.dockerfile.replace("p80.pool.sks-keyservers.net", "keys.openpgp.org")

    logging.info('Rebuilding...')

    build_stream = docker_client.build(fileobj=dockerfile.file, tag=combo_image.image)
    log_stream(build_stream)

    logging.info('Testing image...')

    for i in images:
        test_image(combo_image, i)

    if args.push:
        logging.info('Pushing image...')

        docker_client.login(os.getenv('DOCKER_USERNAME'), os.getenv('DOCKER_PASSWORD'))
        push_stream = docker_client.push('%s/%s' % (combo_image.user, combo_image.repo), combo_image.tag, stream=True)
        log_stream(push_stream)

    return 0


def test_image(combo_image, image):
    cli = image.repo
    version = '--version'
    if image.repo == 'java' or image.repo == 'openjdk':
        cli = 'java'
        version = '-version'
    logging.info(f'{cli} {version}: %s',
                 docker_hl_client.containers.run(
                     combo_image.image, [cli, version], remove=True, stderr=True).decode('utf-8').strip())


if __name__ == '__main__':
    sys.exit(main())
