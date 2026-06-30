"""Apply per-scene speed to an H617A scenceParam blob, the way the Govee app does.

Background (reverse-engineered from Govee Home 7.5.20, class
``com.govee.base2light.ac.diy.speed.SceneSpeedM`` + ``ParamsV2``):

There is no separate BLE "speed" command. Before uploading a dynamic scene, the
app rewrites speed bytes *inside* the scenceParam blob, choosing values from the
scene's ``speedInfo.config`` lookup table at a chosen speed index. The bundled
catalogue blobs ship at a baseline (often the slowest level), so uploading them
verbatim plays some scenes too fast/slow and leaves a few (e.g. Festival -
Carnival, whose movement ships at index 0) looking static. This module reproduces
the app's rewrite so scenes play at their intended speed.

Blob layout (sceneType 2, the RGBIC format ``RgbICEffect.k()`` serialises):
  byte[0] = effect-segment count, then that many segments back to back.
  Each segment:
    [0]   = segLen-1                         (so segLen = blob[S]+1)
    [1]   = packed area nibbles
    [2]   = colour-type selector
    [3:5] = 2 type bytes
    [5]   = brightness "j"
    [6]   = brightness block count nB
    [7 ..]= nB * 6-byte BrightnessEffect blocks; speed field = byte[3] of each
    then  ColorEffect: [icByte][b=SPEED][c][nC] + nC*3 RGB
    then  InAreaMoveEffect: [packed][c][d=SPEED]      (3 bytes)
    then  AreaMoveEffect:   [packed][c][d=SPEED][e]   (4 bytes)

Speed fields written from the per-page table, indexed by the speed level:
  color[idx]   -> ColorEffect.b
  moveIn[idx]  -> InAreaMoveEffect.d
  moveAll[idx] -> AreaMoveEffect.d
  bright[k].brightValue[idx] -> k-th BrightnessEffect.d

Page N in the config maps to effect-segment index N (0-based), matching
SceneSpeedM.g(); pages with no matching segment (and vice-versa) are left alone.
Any structural surprise leaves the blob untouched, so an unexpected format
degrades to current behaviour rather than corrupting the upload.
"""
from __future__ import annotations

import json


def _segment_offsets(blob: bytes):
    """Yield per-segment dicts of absolute byte offsets for the speed fields.

    Raises ValueError if the blob does not match the sceneType-2 layout, so the
    caller can fall back to the unmodified blob.
    """
    if not blob:
        raise ValueError("empty blob")
    count = blob[0]
    off = 1
    for idx in range(count):
        s = off
        if s + 7 > len(blob):
            raise ValueError("segment header past end")
        seg_len = blob[s] + 1
        n_bright = blob[s + 6]
        bright_d = [s + 7 + 6 * i + 3 for i in range(n_bright)]
        color_start = s + 7 + 6 * n_bright
        if color_start + 4 > len(blob):
            raise ValueError("colour block past end")
        color_b = color_start + 1
        n_color = blob[color_start + 3]
        in_start = color_start + 4 + 3 * n_color
        movein_d = in_start + 2
        area_start = in_start + 3
        moveall_d = area_start + 2
        seg_end = area_start + 4
        if seg_end - s != seg_len or seg_end > len(blob):
            raise ValueError("segment length mismatch")
        yield {
            "idx": idx,
            "bright_d": bright_d,
            "color_b": color_b,
            "movein_d": movein_d,
            "moveall_d": moveall_d,
        }
        off = seg_end
    if off != len(blob):
        raise ValueError("trailing bytes after segments")


def _at(arr, index):
    """Value at `index`, clamped into the array; None if no array/values."""
    if not arr:
        return None
    if index < 0:
        index = 0
    elif index >= len(arr):
        index = len(arr) - 1
    return arr[index]


def apply_scene_speed(param: bytes, config: str | None,
                      override_index: int | None = None) -> bytes:
    """Return `param` with speed bytes set per `config`, or `param` unchanged.

    config: the scene's ``speedInfo['config']`` string (JSON list of page dicts).
    override_index: speed level to apply to every page; if None, each page's own
        ``defaultIndex`` is used (the app's default slider position).
    """
    try:
        pages = json.loads(config) if config else None
    except (ValueError, TypeError):
        return param
    if not pages:
        return param

    by_page = {}
    for page in pages:
        try:
            by_page[int(page.get("page"))] = page
        except (TypeError, ValueError):
            continue
    if not by_page:
        return param

    try:
        segments = list(_segment_offsets(param))
    except ValueError:
        return param  # unknown layout -> upload as-is

    blob = bytearray(param)
    for seg in segments:
        page = by_page.get(seg["idx"])
        if page is None:
            continue
        if override_index is not None:
            index = override_index
        else:
            try:
                index = int(page.get("defaultIndex", 0))
            except (TypeError, ValueError):
                index = 0

        color = _at(page.get("color"), index)
        if color is not None:
            blob[seg["color_b"]] = color & 0xFF
        move_in = _at(page.get("moveIn"), index)
        if move_in is not None:
            blob[seg["movein_d"]] = move_in & 0xFF
        move_all = _at(page.get("moveAll"), index)
        if move_all is not None:
            blob[seg["moveall_d"]] = move_all & 0xFF

        bright = page.get("bright") or []
        for k, offset in enumerate(seg["bright_d"]):
            if k < len(bright):
                value = _at(bright[k].get("brightValue"), index)
                if value is not None:
                    blob[offset] = value & 0xFF
    return bytes(blob)
