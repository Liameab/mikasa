"""本地存储（storage package）：SQLite + 索引文件。

存储即目录：data/ 内一个目录即全部状态，备份 = 复制 data/。
"""

from mikasa.storage.index_files import load_index_meta, save_index_meta

__all__ = ["load_index_meta", "save_index_meta"]
