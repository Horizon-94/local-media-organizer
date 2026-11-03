"""Versioned Chinese-script normalization; never translates Latin text or changes timestamps."""
from functools import lru_cache
import ctypes
import sys

POLICY = 'zh-hans-transcription-v1'
WHISPER_PARAMETERS = {'language': 'zh', 'task': 'transcribe',
    'initial_prompt': '以下是中文语音的简体中文转写。保留英文原文、数字和标点。'}

@lru_cache(maxsize=1)
def _corefoundation():
    if sys.platform != 'darwin':
        raise RuntimeError('simplified_chinese_normalizer_requires_macos')
    c = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    ptr=ctypes.c_void_p; size=ctypes.c_long
    c.CFStringCreateWithCString.argtypes=[ptr,ctypes.c_char_p,ctypes.c_uint32];c.CFStringCreateWithCString.restype=ptr
    c.CFStringCreateMutableCopy.argtypes=[ptr,size,ptr];c.CFStringCreateMutableCopy.restype=ptr
    c.CFStringTransform.argtypes=[ptr,ptr,ptr,ctypes.c_bool];c.CFStringTransform.restype=ctypes.c_bool
    c.CFStringGetLength.argtypes=[ptr];c.CFStringGetLength.restype=size
    c.CFStringGetMaximumSizeForEncoding.argtypes=[size,ctypes.c_uint32];c.CFStringGetMaximumSizeForEncoding.restype=size
    c.CFStringGetCString.argtypes=[ptr,ptr,size,ctypes.c_uint32];c.CFStringGetCString.restype=ctypes.c_bool
    c.CFRelease.argtypes=[ptr];c.CFRelease.restype=None
    return c

def simplified_chinese(text):
    if not isinstance(text,str):raise TypeError('transcript_text_must_be_string')
    if '\x00' in text:raise ValueError('transcript_contains_null_character')
    if not text or not any('\u3400'<=ch<='\u9fff' for ch in text):return text
    c=_corefoundation();encoding=0x08000100
    original=c.CFStringCreateWithCString(None,text.encode('utf-8'),encoding)
    transform=c.CFStringCreateWithCString(None,b'Traditional-Simplified',encoding)
    mutable=c.CFStringCreateMutableCopy(None,0,original)
    try:
        if not c.CFStringTransform(mutable,None,transform,False):raise RuntimeError('simplified_chinese_conversion_failed')
        capacity=c.CFStringGetMaximumSizeForEncoding(c.CFStringGetLength(mutable),encoding)+1
        buf=ctypes.create_string_buffer(capacity)
        if not c.CFStringGetCString(mutable,buf,capacity,encoding):raise RuntimeError('simplified_chinese_encoding_failed')
        return buf.value.decode('utf-8')
    finally:
        for value in (mutable,transform,original):
            if value:c.CFRelease(value)

def whisper_parameters(configured):
    # Transcription keeps spoken English; translate would translate speech into English.
    parameters=dict(WHISPER_PARAMETERS);parameters.update(configured or {})
    parameters['language']=parameters.get('language') or 'zh'
    if parameters.get('task')!='transcribe':raise ValueError('whisper_translation_not_supported')
    return parameters
