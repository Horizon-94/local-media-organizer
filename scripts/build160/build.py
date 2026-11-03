"""Build the BUILD160 native executable without copying an App, models or environments."""
from pathlib import Path
import argparse,os,subprocess,sys,platform
ROOT=Path(__file__).resolve().parents[2]
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--derived-data',type=Path,default=ROOT/'build/DerivedData')
    parser.add_argument('--check',action='store_true',help='Run isolated Python regressions before building')
    args=parser.parse_args()
    if sys.platform!='darwin' or platform.machine()!='arm64':parser.error('BUILD160 native frontend requires Apple Silicon macOS and Xcode')
    if args.check:subprocess.run([sys.executable,str(ROOT/'scripts/build160/check.py')],check=True)
    subprocess.run(['xcodebuild','-project',str(ROOT/'macos/Build160.xcodeproj'),'-scheme','Build160Frontend','-configuration','Debug','-derivedDataPath',str(args.derived_data.resolve()),'build','analyze'],check=True)
    print('Native executable:',args.derived_data.resolve()/'Build/Products/Debug/Build160Frontend')
    print('This is a source build artifact, not a downloadable App/DMG or a notarized release.')
if __name__=='__main__':main()
