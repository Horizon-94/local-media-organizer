"""Let existing owner/cancel/deadline signals run during otherwise long SQLite steps.
No timeout changes, TEMP policy changes or database writes.
"""
import sqlite3
_cancelled=False
def cancel():
 global _cancelled
 _cancelled=True
def is_cancelled():return _cancelled
def reset():
 global _cancelled
 _cancelled=False

def install():
 original=sqlite3.connect
 def connect(*args,**kwargs):
  c=original(*args,**kwargs)
  # A Python checkpoint, not busy polling or a replacement for query guards.
  # More specific query progress handlers may still replace this default.
  c.set_progress_handler(lambda:int(_cancelled),10000)
  return c
 sqlite3.connect=connect
 return original
