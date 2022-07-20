[![Actions Status](https://github.com/kichik/docker-combo/workflows/Build%20Images%20Daily/badge.svg)](https://github.com/kichik/docker-combo/actions)

# Docker Combos

Automatically generated and up-to-date Docker combo images of useful official images like Python and Node.

```
$ docker run combos/python_node:3_8 python --version
Python 3.6.2
$ docker run combos/python_node:3_8 node --version
v8.6.0
```

The generated combo images are:

* Always up-to-date
* Using the exact `Dockerfile` as the official images (with minor modifications to facilitate combining two images into one)
* Transparently generated using the source code in this repository
* Pushed to Docker Hub using GitHub Actions where you can verify the process

## Usage

1. Find your language Combo on [Docker Hub](https://hub.docker.com/r/combos/).
1. Find your desired version combo in the Tags tab.
1. Use like any other Docker image (e.g. `docker run combos/python_node:3_8 python -c "print('hello world!')"`)
1. Rest assured the image will always have the latest language versions

## Why is this needed?

Complex projects sometimes require more than one language to run. Examples include Serverless with Python runtime, or Django with Yuglify. Current solutions are to build the combined image yourself which slows the build process, or using a one-off outdated image from Docker Hub created by someone who was in a similar situation a few months ago.

## How does it work?

A cron job is running daily on [GitHub Actions](https://github.com/kichik/docker-combo/actions) and checks if one of the source images in the combos was updated upstream. If its build date is newer than our combo's build date, a new image is built and pushed to Docker Hub. To build the image the `Dockerfile` of both source images are combined by keeping just one `FROM` line and removing all `CMD` and `ENTRYPOINT` lines. The system also verifies that the same `FROM` line is used on both source images to prevent any conflicts.

## Requesting More Combos

To add more combos to be built every day, submit a PR with modifications to `combos.yml`. Add another entry with the format:

```yaml
- images:
    - image1:tag1
    - image2:tag2
  platforms:
    - platform1
    - platform2
```

So if you wanted to add support for Ruby 2.3 and Node 7 running on Alpine for arm64 and amd64, you'd use:

```yaml
- images:
    - ruby:2.3-alpine
    - node:7-alpine
  platforms:
    - linux/arm64/v8
    - linux/amd64
```

After adding the new combo, you should run generate_workflow.py to regenerate the workflow files.

### Some PR Rules:

* All combos must be of commonly used images
* All images must be official and ideally officially supported by Docker itself
* Try to stick to big version scopes (Python 2 instead Python 2.7.11)
* `FROM` line of both images must be identical

## FAQ

**Why are you using GitHub Actions instead of Docker Hub to build?**

> GitHub Actions has support for daily builds so we can keep the images up-to-date. Docker Hub requires a branch per build and doesn't support Dynamic `Dockerfile` generation.

**Why do we have to manually regenerate workflows?**

> Unfortunately, the default action runner does not have the ability to access the permissions needed to automatically generate them.