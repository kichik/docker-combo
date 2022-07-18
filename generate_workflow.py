import argparse
import io
import logging
import os
from pathlib import Path
from ruamel.yaml import YAML
import sys

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)-15s %(levelname)s %(message)s')
    
    yaml = YAML()
    yaml.sort_keys = False
    yaml.width = 5000
    yaml.preserve_quotes = True
    template = yaml.load(io.open("template.yml", "r"))

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
    combos = yaml.load(io.open("combos.yml", "r"))

    for data in combos:
        comboname = generate_image_name(data['images'])
        tag = generate_image_tag(data['images'])
        nametag = comboname + ":" + tag
        template['name'] = nametag
        filename = nametag.replace(":", "_") + "_workflow"
        images = []

        for platform in data["platforms"]:
            images.append(nametag + "-" + platform.replace("/", "_"))

        template["jobs"]["build"]["strategy"]["matrix"]["combo"] = [" ".join(data['images'])]
        template["jobs"]["build"]["strategy"]["matrix"]["platform"] = data["platforms"]
        template["jobs"]["manifest"]["steps"][1]["with"]["base-image"] = nametag
        template["jobs"]["manifest"]["steps"][1]["with"]["extra-images"] = ",".join(images)

        yaml.dump(
            template,
            io.open(filepath + filename.replace("/", "_").replace(":", "_").replace(".", "_") + ".yml", "w")
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
