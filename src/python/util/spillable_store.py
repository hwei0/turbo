"""Abstract base class for in-memory record stores that spill to disk as Parquet files.

SpillableStore accumulates records in memory up to a configurable MAX_ENTRIES threshold,
then flushes them to a numbered Parquet file on disk and clears the in-memory buffer.

Subclasses must override:
  - generate_filepath(): Return the path for the next parquet file
  - append_record(): Add a record to row_list, then call super().append_record()
  - write_to_disk(): Write row_list to parquet using schema, then call super().write_to_disk()

The base class provides:
  - self.row_list: List to accumulate records before writing to disk
  - self.lock: Multiprocessing lock for thread-safe operations
  - clear_store(): Default implementation that clears row_list

Used throughout the system for logging experiment data (client queries, server results,
ping measurements, camera metadata) with bounded memory usage.
"""

from multiprocessing import Lock


class SpillableStore:

    # YOU MUST EXTEND THIS METHOD AND CALL SUPER ON IT
    def __init__(self, MAX_ENTRIES, *args, **kwargs):
        self.fileno = 0
        self.currfile_size = 0
        self.MAX_ENTRIES = MAX_ENTRIES
        self.row_list = []  # Common buffer for accumulating records
        self.lock = Lock()  # Common lock for thread-safe operations

    def generate_filepath(self):
        pass

    # YOU MUST EXTEND THIS METHOD AND CALL SUPER ON IT, *AT THE TAIL*
    def append_record(self, *args, **kwargs):
        self.currfile_size += 1

        if self.currfile_size == self.MAX_ENTRIES:
            self.write_to_disk()

    # Override this method only if you need custom cleanup beyond clearing row_list
    def clear_store(self):
        """Clear the in-memory record buffer. Default implementation clears row_list."""
        self.row_list.clear()

    # YOU MUST EXTEND THIS METHOD AND CALL SUPER ON IT, *AT THE TAIL*
    def write_to_disk(self):
        """Write accumulated records to disk and reset the buffer.

        Subclasses should check if currfile_size > 0, then write row_list to parquet,
        then call super().write_to_disk(). The base implementation handles incrementing
        fileno and clearing the buffer.
        """
        self.fileno += 1
        self.clear_store()
        self.currfile_size = 0

    # extending this one is optional
    def finalize_log(self):
        self.write_to_disk()
