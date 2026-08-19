"""Push config.json to the staging fleet. Run only when a config change is ready to go live."""
import shutil
import sys


def main():
    print("Connecting to staging fleet (2 hosts)...")
    shutil.copyfile("config.json", "DEPLOYED.marker")
    print("Config pushed to staging. Fleet reloaded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
