"""Build, read and round-trip PEST control files from spreadsheet tables."""
try:                                        # the installed package's metadata (pyproject.toml is the source)
    from importlib.metadata import version as _dist_version
    __version__ = _dist_version('makepst')
except Exception:                           # a checkout run without installing
    __version__ = '0.5.0'

from .excel import load_table, to_workbook, update_workbook
from .hpstart import write_hpstart
from .model_files import fill_template, write_number
from .provenance import Manifest
from .pst import Pst, ValidationFix, ValidationReport, read_ensemble, read_obs_ensemble, read_par, read_res
from .reader import from_text, read_pst
from .reweight import balance_weights, discrepancy_weights, equal_shares, scale_weights
from .writer import to_text, write_pst


def to_pyemu(pst):
    from .pyemu_bridge import to_pyemu as _to
    return _to(pst)


def from_pyemu(ppst):
    from .pyemu_bridge import from_pyemu as _from
    return _from(ppst)

__all__ = ['Pst', 'ValidationReport', 'ValidationFix', 'read_pst', 'from_text', 'write_pst', 'to_text',
           'load_table', 'to_workbook', 'update_workbook', 'read_par', 'read_res', 'read_ensemble', 'read_obs_ensemble',
           'fill_template', 'write_number', 'write_hpstart', 'balance_weights', 'discrepancy_weights', 'equal_shares', 'scale_weights', 'Manifest',
           '__version__', 'to_pyemu', 'from_pyemu']
