import sys
sys.path.insert(0, 'target/visidata')

from visidata.threads import codestr

def main():
    try:
        # call site: C:\Users\User\Downloads\proyectos2026\bobhackathon\first-light\target\visidata\visidata/threads.py:436 -- Column('funcname', getter=lambda col,row: codestr(row.code)),
        result = codestr("example_code")
        print(result)
        return 0
    except Exception as e:
        print(e)
        return 1

if __name__ == "__main__":
    sys.exit(main())
