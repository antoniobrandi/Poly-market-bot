"""
Compatibilidad: la implementación vive en scanner15/clob_conn.py (así el servicio
de Railway del scanner se construye solo con la carpeta scanner15/).
Este archivo la carga para quien la importe desde la raíz del bot, por ejemplo
executor.py corriendo en otra carpeta con POLY_BOT_DIR apuntando aquí.
"""
import importlib.util as _ilu
import os as _os

_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "scanner15", "clob_conn.py")
_spec = _ilu.spec_from_file_location("_clob_conn_impl", _path)
_impl = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_impl)
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
BOT_DIR = _os.path.dirname(_os.path.abspath(__file__))   # el .env sigue en la raíz del bot
