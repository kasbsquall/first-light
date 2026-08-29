import sys
sys.path.insert(0, 'target/visidata')

from visidata.color import rgb_to_attr

# call site: C:\Users\User\Downloads\proyectos2026\bobhackathon\first-light\target\visidata\visidata\loaders\xlsx.py:233 -- return rgb_to_attr(int(r, 16), int(g, 16), int(b, 16), int(a, 16))
exit(0 if rgb_to_attr(255, 0, 0, 255) == '40' else 1)
