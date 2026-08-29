import sys
sys.path.insert(0, 'target/visidata')

from visidata.menu import menudraw

def main():
    try:
        menudraw(None, 0, 0, '', '')
        return 0
    except Exception as e:
        print(e)
        return 1

if __name__ == '__main__':
    sys.exit(main())
