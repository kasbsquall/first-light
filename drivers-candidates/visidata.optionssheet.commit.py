import sys
sys.path.insert(0, 'target/visidata')
from visidata.optionssheet import commit

def main():
    # call site: C:\Users\User\Downloads\proyectos2026\bobhackathon\first-light\target\visidata\visidata/optionssheet.py:121 -- commit()
    try:
        commit(None)
        exit(0)
    except Exception as e:
        print(e)
        exit(1)

if __name__ == "__main__":
    main()
