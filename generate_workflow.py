import argparse
import io
import logging
import os
from pathlib import Path
import pprint
import sys
from yaml import load, dump, Loader, SafeDumper

template = {
    "name": "Build Images Daily",
    "on": {
        "push": {},
        "pull_request": {},
        "workflow_dispatch": {
            "inputs" : {
                "forceUpdate": {
                    "description": "Build new images even if base images have not updated",
                    "required": True,
                    "default": False,
                    "type": "boolean"
                }
            }
        },
        "schedule": {
            "cron": "0 0 * * *"
        }
    },
    "jobs": {
        "build": {
            "runs-on": "ubuntu-latest",
            "strategy": {
                "fail-fast": False,
                "matrix": {
                    "combo": [],
                    "platform": [],
                }
            },
            "steps": [
                {
                    "name": "Checkout",
                    "uses": "actions/checkout@v2"
                },
                {
                    "name": "Setup Python",
                    "uses": "actions/setup-python@v1",
                    "with": {
                        "python-version": 3.8
                    }
                },
                {
                    "name": "Install Poetry",
                    "run": "|\n"
                    + "curl -sSL https://raw.githubusercontent.com/python-poetry/poetry/master/get-poetry.py | python -"
                    + "\necho \"$HOME/.poetry/bin\" >> $GITHUB_PATH"
                },
                {
                    "name": "Python Requirements",
                    "run": "|\npoetry install"
                },
                {
                    "name": "Set up QEMU",
                    "id": "qemu",
                    "uses": "docker/setup-qemu-action@8b122486cedac8393e77aa9734c3528886e4a1a8",
                    "with": {
                        "image": "tonistiigi/binfmt:latest",
                        "platforms": "all"
                    }
                },
                {
                    "name": "install buildx",
                    "id": "buildx",
                    "uses": "docker/setup-buildx-action@dc7b9719a96d48369863986a06765841d7ea23f6",
                    "with": {
                        "install": True
                    }
                },
                {
                    "name": "Build",
                    "run": "poetry run python update.py `[ ${GITHUB_REF##*/} == master ] && echo --push` ${{ matrix.combo }} --platform ${{ matrix.platform }} ${{ inputs.forceUpdate && '--force-update' || '' }}",
                    "env": {
                        "DOCKER_USERNAME": "${{ secrets.DOCKERHUB_USERNAME }}",
                        "DOCKER_PASSWORD": "${{ secrets.DOCKERHUB_TOKEN }}"
                    }
                },
                {
                    "name": "Image digests",
                    "run": "docker images --no-trunc"
                }
            ]
        },
        "manifest": {
            "runs-on": "ubuntu-latest",
            "needs": "build",
            "steps": [{
                "name": "Login to Docker Hub",
                "uses": "docker/login-action@d398f07826957cd0a18ea1b059cf1207835e60bc",
                "with": {
                    "DOCKER_USERNAME": "${{ secrets.DOCKERHUB_USERNAME }}",
                    "DOCKER_PASSWORD": "${{ secrets.DOCKERHUB_TOKEN }}"
                }
            },{
                "name": "Create and push manifest images",
                "uses": "Noelware/docker-manifest-action@191bad46d87f7a70c8a82054d4cf98ee8b942dca",
                "with": {
                    "base-image": "",
                    "extra-images": "",
                    "push": True
                }
            }]
        }
    
    }
}

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)-15s %(levelname)s %(message)s')

    filepath = ".github/workflows/"

    # First delete any existing files. This will
    # wipe any combos that should no longer exists.
    #files = glob.glob(filepath + "*_workflow.yml")
    for file in Path(filepath).glob("*_workflow.yml"):
        try:
            file.unlink()
        except OSError as e:
            print("Error: %s : %s" % (f, e.strerror))

    # Now create the new files
    combos = load(io.open("combos.yml", "r"), Loader=Loader)
    for data in combos:
        comboname = generate_image_name(data['images'])
        tag = generate_image_tag(data['images'])
        nametag = comboname + ":" + tag
        filename = nametag.replace(":", "_") + "_workflow"
        images = []

        for platform in data["platforms"]:
            images.append(nametag + "-" + platform.replace("/", "_"))

        template["jobs"]["build"]["strategy"]["matrix"]["combo"] = [" ".join(data['images'])]
        template["jobs"]["build"]["strategy"]["matrix"]["platform"] = data["platforms"]
        template["jobs"]["manifest"]["steps"][1]["with"]["base-image"] = nametag
        template["jobs"]["manifest"]["steps"][1]["with"]["extra-images"] = ",".join(images)

        dump(
            template,
            io.open(filepath + filename.replace("/", "_").replace(":", "_").replace(".", "_") + ".yml", "w"),
            Dumper=SafeDumper,
            sort_keys=False,
            allow_unicode=True,
            width=5000,
            line_break='\n'
        )

def generate_image_name(combos):
    combos = combos.copy()
    name = combos.pop(0).split(":")[0]
    
    for image in combos:
        name += "_" + image.split(":")[0]
    
    return "combos/" + name

def generate_image_tag(combos):
    combos = combos.copy()
    tag = combos.pop(0).split(":")[1].split("@")[0].split(" ")[0]
    
    for image in combos:
        tag += "_" + image.split(":")[1].split("@")[0].split(" ")[0]
    
    return tag

if __name__ == '__main__':
    sys.exit(main())
