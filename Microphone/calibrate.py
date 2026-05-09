"""會議前麥克風校準工具。

四個 phase 測完，自動寫 calibration.json；app.py 啟動時自動載入覆蓋預設值。

用法：
    python calibrate.py              # 完整校準
    python calibrate.py --live       # 即時 RMS 監測
    python calibrate.py --quick      # 跳過 VAD mode 測試（更快）
"""
import argparse
import datetime
import json
import os
import time

import numpy as np
import sounddevice as sd
import webrtcvad


CAPTURE_RATE = 48000
EMIT_RATE = 16000
DOWNSAMPLE = 3
BLOCK_SAMPLES_CAPTURE = 1440      # 30ms @ 48kHz
BLOCK_SAMPLES_EMIT = 480          # 30ms @ 16kHz

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
CALIBRATION_PATH = os.path.join(THIS_DIR, "calibration.json")


# ---------- Helpers ----------

def find_input_device():
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            return i, d
    return None, None


def chunk_to_int16_16k(indata):
    """跟 app.py 一致：48k float32 → 16k int16 stride-3"""
    chunk_int16_48k = (indata[:, 0] * 32767).astype(np.int16)
    return chunk_int16_48k[::DOWNSAMPLE]


def chunk_rms(chunk_int16):
    return int(np.sqrt(np.mean(chunk_int16.astype(np.int32) ** 2)))


def countdown(secs, label):
    """倒數 + 進度條"""
    bar_w = 40
    for i in range(secs * 10):
        elapsed = i / 10.0
        pct = elapsed / secs
        filled = int(bar_w * pct)
        bar = "█" * filled + "░" * (bar_w - filled)
        print(f"\r  {label}  [{bar}] {elapsed:>4.1f}/{secs}s", end="", flush=True)
        time.sleep(0.1)
    print()


# ---------- Phases ----------

def phase1_device_check():
    print("=" * 60)
    print("Phase 1：裝置檢查")
    print("=" * 60)

    device_idx, device_info = find_input_device()
    if device_idx is None:
        print("❌ 找不到任何 input device！檢查 USB mic 是否插好。")
        return None
    print(f"✅ Input device: [{device_idx}] {device_info['name']}")
    print(f"   max_input_channels: {device_info['max_input_channels']}")
    print(f"   default_samplerate: {int(device_info['default_samplerate'])} Hz")

    # 嘗試開 stream
    try:
        with sd.InputStream(device=device_idx, samplerate=CAPTURE_RATE, channels=1,
                            blocksize=BLOCK_SAMPLES_CAPTURE, dtype="float32"):
            pass
        print(f"✅ {CAPTURE_RATE} Hz mono stream 可開啟")
    except Exception as e:
        print(f"❌ 無法開 {CAPTURE_RATE} Hz stream: {e}")
        return None
    return device_idx


def phase2_background(device, duration=15):
    print()
    print("=" * 60)
    print(f"Phase 2：背景噪音測量（請保持完全安靜）")
    print("=" * 60)
    print(f"  即將開始錄音 {duration}s。請不要說話、不要敲鍵盤、不要動椅子。")
    input("  按 Enter 開始...")

    chunks = []
    rms_samples = []

    def cb(indata, frames, time_info, status):
        c16 = chunk_to_int16_16k(indata)
        chunks.append(c16.copy())
        rms_samples.append(chunk_rms(c16))

    with sd.InputStream(device=device, samplerate=CAPTURE_RATE, channels=1,
                        blocksize=BLOCK_SAMPLES_CAPTURE, dtype="float32", callback=cb):
        countdown(duration, "錄背景")

    rms = np.array(rms_samples)
    stats = {
        "min": int(rms.min()),
        "p50": int(np.percentile(rms, 50)),
        "p95": int(np.percentile(rms, 95)),
        "p99": int(np.percentile(rms, 99)),
        "max": int(rms.max()),
        "mean": int(rms.mean()),
    }
    print(f"  → 背景 RMS  min={stats['min']}  p50={stats['p50']}  "
          f"p95={stats['p95']}  p99={stats['p99']}  max={stats['max']}")

    if stats["p99"] > 800:
        print("  ⚠️  背景偏吵（p99 > 800）；建議改善環境或降低 mic 增益（alsamixer）")

    return stats, chunks


def phase3_voice(device, duration=15):
    print()
    print("=" * 60)
    print(f"Phase 3：人聲測量")
    print("=" * 60)
    print(f"  即將錄音 {duration}s。請以正常會議音量自然講話，可以朗讀任何文字。")
    print(f"  範例：「Hello, my name is Alice. I work on distributed systems at Acme Corp.")
    print(f"          The quick brown fox jumps over the lazy dog.」")
    input("  按 Enter 開始...")

    rms_samples = []
    clip_count = 0

    def cb(indata, frames, time_info, status):
        c16 = chunk_to_int16_16k(indata)
        rms_samples.append(chunk_rms(c16))
        nonlocal_clip = np.abs(c16).max()
        if nonlocal_clip > 30000:                # 接近 int16 上限 32767 = clipping
            nonlocal clip_count
            clip_count += 1

    with sd.InputStream(device=device, samplerate=CAPTURE_RATE, channels=1,
                        blocksize=BLOCK_SAMPLES_CAPTURE, dtype="float32", callback=cb):
        countdown(duration, "錄人聲")

    rms = np.array(rms_samples)
    # 過濾過低值（停頓 / 換氣）以估「實際發聲」分布
    voiced = rms[rms > rms.mean() * 0.3]
    if len(voiced) == 0:
        voiced = rms

    stats = {
        "min": int(rms.min()),
        "p20": int(np.percentile(voiced, 20)),    # 軟發聲
        "p50": int(np.percentile(voiced, 50)),    # 常態
        "p95": int(np.percentile(voiced, 95)),    # 重音
        "max": int(rms.max()),
        "mean": int(voiced.mean()),
        "clip_frames": clip_count,
        "total_frames": len(rms_samples),
    }
    print(f"  → 人聲 RMS  voiced_p20={stats['p20']}  voiced_p50={stats['p50']}  "
          f"voiced_p95={stats['p95']}")

    if stats["p50"] < 1500:
        print("  ⚠️  人聲偏弱（p50 < 1500）；建議講大聲、靠近 mic、或調高 alsamixer mic 增益")
    if clip_count > stats["total_frames"] * 0.05:
        print(f"  ⚠️  {clip_count} 個 chunk 偵測到 clipping（音爆破）；建議降低 mic 增益")

    return stats


def phase4_vad_modes(bg_chunks, target_max_fpr=0.02):
    """對背景 chunk 跑 VAD mode 0-3，看誰 false-positive rate 最低"""
    print()
    print("=" * 60)
    print("Phase 4：VAD mode 自動選擇")
    print("=" * 60)
    print(f"  測試 WebRTC VAD mode 0-3 在背景下的 false positive rate...")

    results = {}
    for mode in [0, 1, 2, 3]:
        vad = webrtcvad.Vad(mode)
        speech_count = 0
        for c in bg_chunks:
            if len(c) != BLOCK_SAMPLES_EMIT:
                continue
            try:
                if vad.is_speech(c.tobytes(), EMIT_RATE):
                    speech_count += 1
            except Exception:
                pass
        fpr = speech_count / max(1, len(bg_chunks))
        results[mode] = fpr
        marker = "✅" if fpr <= target_max_fpr else "❌"
        print(f"  {marker} mode {mode}: false-positive {fpr*100:.1f}% ({speech_count}/{len(bg_chunks)})")

    # 選擇：滿足 target_max_fpr 的最低 mode（不過度過濾）；都不滿足就 mode 3
    chosen = 3
    for mode in [0, 1, 2, 3]:
        if results[mode] <= target_max_fpr:
            chosen = mode
            break
    print(f"  → 選擇 mode {chosen}（false-positive ≤ {target_max_fpr*100:.0f}% 的最低 mode）")
    return chosen, results


# ---------- Compute calibration ----------

def compute_calibration(bg, voice, vad_mode):
    """從測量結果推導參數值"""
    # SILENCE_FLOOR：略高於背景 noise floor，但低於最弱人聲
    silence_floor = max(100, min(int(bg["p99"] * 1.3), int(voice["p20"] * 0.4)))

    # RMS_GATE：擋背景，但低於常態人聲（取軟發聲 p20 × 0.7 與背景 p95 × 2.5 的較小者）
    rms_gate = max(silence_floor + 200,
                   min(int(voice["p20"] * 0.7), int(bg["p95"] * 2.5)))

    return {
        "VAD_MODE": vad_mode,
        "RMS_GATE": rms_gate,
        "SILENCE_FLOOR": silence_floor,
        "MIN_SILENCE_MS": 1200,        # 預設值；自然停頓量測較難 robust，沿用
        "START_CONFIRM_FRAMES": 6,     # 180ms confirm，pre-roll 已 cover
        "MIN_UTTERANCE_MS": 1500,      # 過短 utterance 易致 Whisper 幻覺
    }


def show_report(bg, voice, vad_results, calib):
    print()
    print("=" * 60)
    print("最終校準報告")
    print("=" * 60)
    print(f"  背景：    p50={bg['p50']:<5} p95={bg['p95']:<5} p99={bg['p99']:<5} max={bg['max']}")
    print(f"  人聲：    p20={voice['p20']:<5} p50={voice['p50']:<5} p95={voice['p95']}")
    print(f"  Clipping：{voice['clip_frames']}/{voice['total_frames']} frames")
    print()
    print("  推薦參數：")
    for k, v in calib.items():
        print(f"    {k:<22} = {v}")
    print()
    print(f"  健康檢查：")
    pass_silence = bg["p99"] < calib["SILENCE_FLOOR"]
    pass_gate = calib["RMS_GATE"] < voice["p20"]
    pass_separation = calib["SILENCE_FLOOR"] < calib["RMS_GATE"]
    print(f"    背景 p99 < SILENCE_FLOOR   {'✅' if pass_silence else '❌'} ({bg['p99']} < {calib['SILENCE_FLOOR']})")
    print(f"    SILENCE_FLOOR < RMS_GATE  {'✅' if pass_separation else '❌'}")
    print(f"    RMS_GATE < 人聲 p20       {'✅' if pass_gate else '❌'} ({calib['RMS_GATE']} < {voice['p20']})")
    if not (pass_silence and pass_gate and pass_separation):
        print("    ⚠️  某些檢查未通過，可能是環境吵 / 人聲弱；參數仍寫入但效果可能不佳")


def write_calibration(calib, bg, voice):
    out = {
        **calib,
        "_meta": {
            "calibrated_at": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "background_rms": bg,
            "voice_rms": voice,
        },
    }
    with open(CALIBRATION_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n✅ 已寫入 {CALIBRATION_PATH}")
    print("   下次 python app.py 會自動載入。")


# ---------- Live mode ----------

def live(device):
    print("Live RMS 監測（Ctrl+C 退出）...")
    bar_unit = 50

    def cb(indata, frames, time_info, status):
        c16 = chunk_to_int16_16k(indata)
        rms = chunk_rms(c16)
        bar = "█" * min(rms // bar_unit, 60)
        print(f"\rRMS={rms:<6} {bar:<60}", end="", flush=True)

    with sd.InputStream(device=device, samplerate=CAPTURE_RATE, channels=1,
                        blocksize=BLOCK_SAMPLES_CAPTURE, dtype="float32", callback=cb):
        try:
            while True:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print()


# ---------- Main ----------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="即時 RMS 監測")
    parser.add_argument("--quick", action="store_true", help="跳過 VAD mode 測試")
    parser.add_argument("--bg-secs", type=int, default=15)
    parser.add_argument("--voice-secs", type=int, default=15)
    args = parser.parse_args()

    device = phase1_device_check()
    if device is None:
        exit(1)

    if args.live:
        live(device)
        exit(0)

    bg, bg_chunks = phase2_background(device, args.bg_secs)
    voice = phase3_voice(device, args.voice_secs)

    if args.quick:
        vad_mode = 3
        vad_results = {3: -1}
    else:
        vad_mode, vad_results = phase4_vad_modes(bg_chunks)

    calib = compute_calibration(bg, voice, vad_mode)
    show_report(bg, voice, vad_results, calib)

    confirm = input("\n寫入 calibration.json 嗎？[Y/n] ").strip().lower()
    if confirm in ("", "y", "yes"):
        write_calibration(calib, bg, voice)
    else:
        print("已取消，未寫入。")
