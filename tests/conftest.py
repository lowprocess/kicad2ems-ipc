"""The plugin from plugin/ (as KiCad loads it: the folder on sys.path), kicad2ems from the environment."""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'plugin'))
try:                                    # the window's test: shared OpenGL contexts, as kicad2ems's GUI makes them
    from PySide6.QtCore import QCoreApplication, Qt
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
except ImportError:
    pass
