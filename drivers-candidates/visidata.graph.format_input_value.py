import sys
sys.path.insert(0, 'target/visidata')

from visidata.graph import format_input_value

def main():
    try:
        # call site: C:\Users\User\Downloads\proyectos2026\bobhackathon\first-light\target\visidata\visidata\graph.py:324 --        suggested = format_input_value(val, xtype)
        result1 = format_input_value('2023-01-01', 'date')
        result2 = format_input_value(123, 'int')
        result3 = format_input_value('example', 'str')
        
        print("Function executed successfully with test inputs.")
        return 0
    except Exception as e:
        print(f"Function execution failed: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
