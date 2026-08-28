"""Bounded, read-only extraction of embedded camera LTC metadata.

FFmpeg's RTMD header timecode can disagree with the embedded LTC drop flag.
Use the frame-zero NonRealTimeMeta table, never infer DF from camera/FPS alone.
Reference: CommandPost Sony Timecode Toolbox (LtcChangeTable, FFSSMMHH,
halfStep); independently implemented, without copying its repair workflow.
https://github.com/CommandPost/CommandPost/blob/develop/src/plugins/finalcutpro/toolbox/sonytimecode/init.lua
"""
from fractions import Fraction
from pathlib import Path
import re
import struct
from xml.etree import ElementTree as ET


def embedded_ltc(path: Path, video_rate: Fraction) -> tuple[str, Fraction]:
    # Seek over mdat; do not read/decode video, crawl directories, or edit files.
    document = None
    with path.open('rb') as stream:
        size = stream.seek(0, 2)
        offset = 0
        for _ in range(256):
            if offset + 8 > size:
                break
            stream.seek(offset)
            length, kind = struct.unpack('>I4s', stream.read(8))
            header = 8
            if length == 1:
                extra = stream.read(8)
                if len(extra) != 8:
                    break
                length = struct.unpack('>Q', extra)[0]
                header = 16
            if length == 0:
                length = size - offset
            if length < header or offset + length > size:
                break
            if kind in {b'meta', b'uuid', b'moov'} and length <= 4 * 1024 * 1024:
                data = stream.read(length - header)
                begin = data.find(b'<NonRealTimeMeta')
                end = data.find(b'</NonRealTimeMeta>', begin)
                if begin >= 0 and end >= 0:
                    xml = data[begin:end + len(b'</NonRealTimeMeta>')]
                    document = ET.fromstring(xml)
                    break
            offset += length
    if document is None:
        raise ValueError('RTMD 原片缺少可核实的内嵌 LTC 时间码；不能猜测丢帧设置')
    nodes = {node.tag.rsplit('}', 1)[-1]: node for node in document.iter()}
    table = nodes.get('LtcChangeTable')
    video = nodes.get('VideoFrame')
    if table is None or video is None:
        raise ValueError('内嵌 LTC 时间码表不完整')
    first = next((n for n in table if n.get('frameCount') == '0' and n.get('status') == 'increment'), None)
    value = first.get('value', '') if first is not None else ''
    if not re.fullmatch(r'[0-9a-fA-F]{8}', value):
        raise ValueError('内嵌 LTC 缺少有效的第零帧时间码')
    tc_fps = int(table.get('tcFps', '0'))
    half_step = table.get('halfStep')
    if half_step not in {'true', 'false'} or tc_fps <= 0:
        raise ValueError('内嵌 LTC 帧率或 halfStep 无效')
    multiplier = 2 if half_step == 'true' else 1
    fps_text = re.fullmatch(r'(\d+(?:\.\d+)?)[pi]', video.get('formatFps', ''))
    if fps_text is None:
        raise ValueError('内嵌 LTC 缺少有效的录制帧率')
    rate = {'23.976': Fraction(24000, 1001), '29.97': Fraction(30000, 1001),
            '59.94': Fraction(60000, 1001)}.get(fps_text[1], Fraction(fps_text[1]))
    if rate != video_rate or tc_fps * multiplier != round(rate):
        raise ValueError('内嵌 LTC 与视频帧率冲突，需要复核原片')
    raw = bytes.fromhex(value)
    def bcd(byte, mask):
        byte &= mask
        if byte & 15 > 9 or byte >> 4 > 9:
            raise ValueError('内嵌 LTC BCD 编码无效')
        return (byte >> 4) * 10 + (byte & 15)
    frame, second, minute, hour = [bcd(b, m) for b, m in zip(raw, (0x3f, 0x7f, 0x7f, 0x3f))]
    drop = bool(raw[0] & 0x40)
    if hour > 23 or minute > 59 or second > 59 or frame >= tc_fps:
        raise ValueError('内嵌 LTC 时间码越界')
    if drop and rate not in {Fraction(30000, 1001), Fraction(60000, 1001)}:
        raise ValueError('内嵌 LTC 丢帧标记与帧率冲突')
    sep = ';' if drop else ':'
    return f'{hour:02}:{minute:02}:{second:02}{sep}{frame * multiplier:02}', rate
