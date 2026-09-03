# 一键发自动混剪系统配音 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有自动混剪中增加完全离线的系统中文配音模式，让同一批成片共用一条完整解说音轨，并以配音时长驱动镜头计划和结果回读。

**Architecture:** 新增独立 `narration_service` 负责 macOS/Windows 系统声音发现、一次合成、FFmpeg 规范化和音轨回读；现有批次服务在素材探测后、镜头规划前调用它，并把实际主音轨时长写入一个仅供本批规划使用的请求副本。现有渲染器在配音模式下始终静音切片，合并画面后以流复制方式复用批次主音轨，UI 与批次回执只消费同一套服务结果。

**Tech Stack:** Python 3.11、PyQt6、`unittest`、FFmpeg/FFprobe、macOS `/usr/bin/say`、Windows PowerShell 5.1 `System.Speech`

**Spec:** `docs/superpowers/specs/2026-09-03-oneclick-automatic-montage-system-narration-design.md`

## Global Constraints

- 只允许本机离线配音；不增加网络请求、云端语音服务、API Key 或新 Python 依赖。
- macOS 只调用 `/usr/bin/say`；Windows 只调用 Windows PowerShell 和 `System.Speech.Synthesis.SpeechSynthesizer`。
- 中文声音优先简体中文，其次其他 `zh-*`；没有中文声音必须返回 `montage_narration_voice_unavailable`，不得回退英文。
- 配音原文只能通过临时 UTF-8 文件传给系统工具，不得进入命令行参数或普通运行日志。
- 一个批次只合成一次配音；所有成片必须引用同一个 `narration.sha256` 和 `stream_sha256`。
- 有效成片时长固定为 `max(5000, speech_duration_ms + 300)`；超过 `180000` 毫秒时在渲染前失败。
- 配音模式始终移除素材原声，不做混音，不做背景音乐，不生成字幕，不进入发布任务。
- P002 的 VoxCPM2 音色和资产保持隔离，不读取、不复制、不改动。
- 当前工作树已有未提交的自动混剪与其他用户改动；不得 `reset`、`checkout` 或覆盖，提交时只暂存当前任务的精确文件或精确补丁块。
- 本功能线不递增客户端版本、不打包、不推送、不操作平台；Mac 源码真实生成通过也不能冒充 Windows 安装包可用。

---

### Task 0: 先冻结已验证的自动混剪基线

**Files:**
- Add: `app_core/montage_models.py`
- Add: `app_core/montage_runtime.py`
- Add: `app_core/montage_service.py`
- Add: `ui/automatic_montage_page.py`
- Add: `test_montage_models.py`
- Add: `test_montage_runtime.py`
- Add: `test_montage_service.py`
- Add: `test_automatic_montage_page.py`
- Modify: `app_core/paths.py`
- Existing documentation checkpoint: `README.md`、`QUALITY_GATES.md`、`SOURCE_OF_TRUTH.md`

**Purpose:** 当前上述混剪源码仍是未跟踪文件；若直接从 Task 1 开始，第一次配音提交会吞入整套旧基线。先把已经通过 `30/30` 定向测试和 `3135/3135` 完整回归的现状独立冻结，后续每个配音提交才可审查、可回滚。

- [ ] **Step 1: 确认索引为空并记录仍需保留的其他工作树改动**

Run:

```bash
git diff --cached --name-only
git status --short
```

Expected: 索引为空；除本任务列出的混剪文件外，账号、Meta 入口、主窗口和其他未跟踪产物仍保持原状，不删除、不移动、不暂存。

- [ ] **Step 2: 复跑现有混剪基线测试**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v \
  test_montage_models test_montage_runtime \
  test_montage_service test_automatic_montage_page
```

Expected: `30/30` PASS；若实际数量已经变化，记录本次真实数量并以零失败、零错误为准。

- [ ] **Step 3: 只提交自包含的混剪核心和运行目录常量**

```bash
git add \
  app_core/montage_models.py app_core/montage_runtime.py app_core/montage_service.py \
  app_core/paths.py ui/automatic_montage_page.py \
  test_montage_models.py test_montage_runtime.py test_montage_service.py \
  test_automatic_montage_page.py
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: checkpoint local automatic montage core"
```

Expected staged names: 以上 9 个文件，不能出现 `account_service`、`main_window`、Meta 测试、`outputs/` 或视觉检查临时产物。

- [ ] **Step 4: 独立冻结现有状态文档，避免 Task 7 吞入旧差异**

先运行：

```bash
git diff -- README.md QUALITY_GATES.md SOURCE_OF_TRUTH.md
```

只在差异仍严格对应以下已确认事实时继续：`0.5.31` 已安装、Meta 入口处于未发布源码隐藏状态、自动混剪基线及其测试和真实素材批次已记录。然后运行：

```bash
git add README.md QUALITY_GATES.md SOURCE_OF_TRUTH.md
git diff --cached --check
git commit -m "docs: record current integration and montage baseline"
```

若出现新的未知事实或别的任务内容，则不暂存这三份文档，并在 Task 7 使用精确补丁暂存本轮新增段落。

- [ ] **Step 5: 确认基线提交没有带走其余用户改动**

Run:

```bash
git log -2 --stat --oneline
git status --short
```

Expected: 两个基线提交可分别审查；原有账号、Meta、主窗口接线和其他未跟踪产物仍在工作树中。

### Task 1: 扩展混剪请求合同

**Files:**
- Modify: `app_core/montage_models.py:15-190`
- Test: `test_montage_models.py:16-95`

**Interfaces:**
- Consumes: 现有 `MontageRequest.from_mapping(raw: Mapping[str, object]) -> MontageRequest`
- Produces: `MontageRequest.narration_text: str`；合法 `audio_mode` 集合变为 `{"mute", "source", "narration"}`

- [ ] **Step 1: 在测试请求中加入默认空配音文案并写失败用例**

```python
def request(self, **overrides: object) -> MontageRequest:
    raw = {
        # 保留现有字段
        "audio_mode": "mute",
        "narration_text": "",
    }
    raw.update(overrides)
    return MontageRequest.from_mapping(raw)

def test_narration_mode_requires_text_and_normalizes_it(self) -> None:
    with self.assertRaises(MontageFailure) as missing:
        self.request(audio_mode="narration", narration_text="  ")
    self.assertEqual(missing.exception.code, "montage_narration_text_required")

    request = self.request(
        audio_mode="narration",
        narration_text="  你好，这是一段解说。  ",
    )
    self.assertEqual(request.narration_text, "你好，这是一段解说。")
    self.assertFalse(request.source_audio_confirmed)
    self.assertEqual(request.to_dict()["narration_text"], "你好，这是一段解说。")

def test_non_narration_mode_ignores_stale_narration_text(self) -> None:
    request = self.request(audio_mode="mute", narration_text="不会参与生成")
    self.assertEqual(request.narration_text, "")

def test_narration_mode_does_not_compare_clip_with_ignored_ui_target(self) -> None:
    request = self.request(
        audio_mode="narration",
        narration_text="这段配音最终会超过五秒。",
        target_duration_ms=5_000,
        clip_duration_ms=10_000,
    )
    self.assertEqual(request.clip_duration_ms, 10_000)
```

- [ ] **Step 2: 运行新增请求测试并确认旧模型拒绝 `narration`**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_montage_models.MontageModelsTests.test_narration_mode_requires_text_and_normalizes_it \
  test_montage_models.MontageModelsTests.test_non_narration_mode_ignores_stale_narration_text \
  test_montage_models.MontageModelsTests.test_narration_mode_does_not_compare_clip_with_ignored_ui_target
```

Expected: FAIL；旧模型仍拒绝 `narration`，也没有 `narration_text` 字段和“配音模式忽略旧总时长”的合同。

- [ ] **Step 3: 实现最小请求字段和稳定校验**

```python
REQUEST_FIELDS = frozenset(
    {
        # 保留现有字段
        "narration_text",
    }
)

@dataclass(frozen=True, slots=True)
class MontageRequest:
    # 保留现有字段顺序
    narration_text: str

# 在 from_mapping 的 audio_mode 校验处：
audio_mode = str(raw.get("audio_mode") or "").strip().lower()
if audio_mode not in {"mute", "source", "narration"}:
    raise MontageFailure(
        "montage_audio_mode_invalid",
        "音频模式只能是静音、保留环境原声或系统自动配音",
    )

narration_value = raw.get("narration_text", "")
if not isinstance(narration_value, str):
    raise MontageFailure("montage_narration_text_required", "配音文案必须是文字")
narration_text = narration_value.strip()
if audio_mode == "narration" and not narration_text:
    raise MontageFailure("montage_narration_text_required", "请输入需要配音的解说文案")
if audio_mode != "narration":
    narration_text = ""

# 原有 clip_duration_ms 范围校验只保留 500–10000ms；把“不能超过总时长”
# 延后到 audio_mode 解析之后。narration 的目标时长尚未生成，不能与隐藏的
# target_duration_ms 比较；mute/source 继续执行原校验。
if audio_mode != "narration" and clip_duration_ms > target_duration_ms:
    raise MontageFailure(
        "montage_clip_duration_invalid",
        "镜头时长不能超过成片时长",
    )

# cls(...) 构造和 to_dict() 都加入 narration_text=narration_text。
```

- [ ] **Step 4: 运行模型模块全部测试**

Run: `.venv/bin/python -m unittest -v test_montage_models`

Expected: PASS；原有静音、环境原声、容量和文案变体行为不变。

- [ ] **Step 5: 提交请求合同**

```bash
git add app_core/montage_models.py test_montage_models.py
git diff --cached --check
git commit -m "feat: add montage narration request contract"
```

### Task 2: 建立双平台本机配音服务

**Files:**
- Create: `app_core/narration_service.py`
- Create: `test_narration_service.py`

**Interfaces:**
- Consumes: `MontageFailure`；具有 `ffmpeg: Path`、`ffprobe: Path` 的运行时对象
- Produces: `SystemVoice`、`AudioReadback`、`NarrationArtifact`、`choose_chinese_voice()`、`probe_audio_readback()`、`synthesize_system_narration()`

- [ ] **Step 1: 写声音优先级、无中文声音和安全传参测试**

新建 `NarrationServiceTests(unittest.TestCase)`，使用与其他混剪测试相同的临时目录生命周期；以下 `test_*` 均放入该类：

```python
from pathlib import Path
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from app_core.montage_models import MontageFailure
from app_core.narration_service import (
    AudioReadback,
    SystemVoice,
    choose_chinese_voice,
    probe_audio_readback,
    synthesize_system_narration,
    _parse_macos_voices,
    _parse_windows_voices,
    _synthesize_macos_raw,
    _synthesize_windows_raw,
)

class NarrationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.runtime = SimpleNamespace(
            ffmpeg=self.root / "ffmpeg",
            ffprobe=self.root / "ffprobe",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

# 将下列 test_* 定义放在 NarrationServiceTests 内并保持四空格缩进。

def test_choose_chinese_voice_prefers_simplified_chinese(self) -> None:
    selected = choose_chinese_voice(
        (
            SystemVoice("English", "en-US"),
            SystemVoice("Taiwan", "zh-TW"),
            SystemVoice("Mainland", "zh-CN"),
        )
    )
    self.assertEqual(selected.id, "Mainland")

def test_choose_chinese_voice_never_falls_back_to_english(self) -> None:
    with self.assertRaises(MontageFailure) as caught:
        choose_chinese_voice((SystemVoice("English", "en-US"),))
    self.assertEqual(caught.exception.code, "montage_narration_voice_unavailable")

def test_macos_and_windows_voice_lists_parse_to_the_same_contract(self) -> None:
    mac = _parse_macos_voices(
        "Ting-Ting             zh_CN    # 你好！我是婷婷。\n"
        "Samantha              en_US    # Hello! My name is Samantha.\n"
    )
    windows = _parse_windows_voices(
        '[{"id":"Microsoft Huihui Desktop","locale":"zh-CN"}]'
    )
    self.assertEqual(mac[0], SystemVoice("Ting-Ting", "zh-CN"))
    self.assertEqual(windows, (SystemVoice("Microsoft Huihui Desktop", "zh-CN"),))

def test_macos_and_windows_pass_text_by_file_not_command_line(self) -> None:
    secret_text = "这段文案不能进入命令行"
    text_path = self.root / "narration.txt"
    text_path.write_text(secret_text, encoding="utf-8")
    voice = SystemVoice("Test Chinese", "zh-CN")
    mac_output = self.root / "raw.aiff"
    windows_output = self.root / "raw.wav"
    commands: list[list[str]] = []

    def mac_runner(command: list[str], **_kwargs: object):
        commands.append(command)
        mac_output.write_bytes(b"aiff")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def windows_runner(command: list[str], **_kwargs: object):
        commands.append(command)
        windows_output.write_bytes(b"wave")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    _synthesize_macos_raw(text_path, mac_output, voice, runner=mac_runner)
    _synthesize_windows_raw(text_path, windows_output, voice, runner=windows_runner)

    self.assertTrue(mac_output.is_file())
    self.assertTrue(windows_output.is_file())
    self.assertTrue(all(secret_text not in argument for command in commands for argument in command))
    self.assertTrue(all(str(text_path) in command for command in commands))
```

- [ ] **Step 2: 运行新模块测试并确认模块尚不存在**

Run:

```bash
.venv/bin/python -m unittest -v test_narration_service
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app_core.narration_service'`。

- [ ] **Step 3: 定义不可变返回合同与中文声音选择函数**

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

class AudioRuntime(Protocol):
    ffmpeg: Path
    ffprobe: Path

@dataclass(frozen=True, slots=True)
class SystemVoice:
    id: str
    locale: str

@dataclass(frozen=True, slots=True)
class AudioReadback:
    duration_ms: int
    stream_sha256: str

@dataclass(frozen=True, slots=True)
class NarrationArtifact:
    master_path: Path
    voice_id: str
    voice_locale: str
    speech_duration_ms: int
    master_duration_ms: int
    sha256: str
    stream_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "success",
            "master_path": str(self.master_path),
            "voice_id": self.voice_id,
            "voice_locale": self.voice_locale,
            "speech_duration_ms": self.speech_duration_ms,
            "master_duration_ms": self.master_duration_ms,
            "sha256": self.sha256,
            "stream_sha256": self.stream_sha256,
        }

def choose_chinese_voice(voices: Sequence[SystemVoice]) -> SystemVoice:
    def rank(voice: SystemVoice) -> tuple[int, str, str]:
        locale = voice.locale.replace("_", "-").casefold()
        priorities = {"zh-cn": 0, "zh-sg": 1, "zh-tw": 2, "zh-hk": 3}
        return (priorities.get(locale, 4), locale, voice.id.casefold())

    chinese = [voice for voice in voices if voice.locale.replace("_", "-").casefold().startswith("zh-")]
    if not chinese:
        raise MontageFailure("montage_narration_voice_unavailable", "本机没有可用的中文系统声音")
    return min(chinese, key=rank)
```

- [ ] **Step 4: 实现 macOS 与 Windows 声音发现和原始音频生成**

macOS 合成命令必须采用参数数组：

```python
command = [
    "/usr/bin/say",
    "-v", voice.id,
    "-f", str(text_path),
    "-o", str(raw_output_path),
]
```

Windows 合成脚本固定为：

```powershell
param([string]$TextPath, [string]$OutputPath, [string]$VoiceName)
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $synth.SelectVoice($VoiceName)
  $text = [IO.File]::ReadAllText($TextPath, [Text.Encoding]::UTF8)
  $synth.SetOutputToWaveFile($OutputPath)
  $synth.Speak($text)
} finally {
  $synth.Dispose()
}
```

调用命令固定为：

```python
command = [
    "powershell.exe", "-NoProfile", "-NonInteractive",
    "-ExecutionPolicy", "Bypass", "-File", str(script_path),
    str(text_path), str(raw_output_path), voice.id,
]
```

macOS 声音发现解析 `/usr/bin/say -v ?` 的名称和 `zh_CN/zh_TW/zh_HK` 区域。Windows 枚举脚本用 `@(...) | ConvertTo-Json -Compress` 强制输出 JSON 数组，每项只包含 `id=VoiceInfo.Name` 与 `locale=VoiceInfo.Culture.Name`，避免机器仅有一个声音时返回对象导致解析分叉。非 macOS/Windows 平台抛出 `montage_narration_platform_unsupported`；进程非零退出、超时、目标不存在或零字节统一抛出 `montage_narration_synthesis_failed`。

- [ ] **Step 5: 写音频回读、规范化和时长边界测试**

```python
def test_master_duration_is_speech_plus_tail_with_five_second_floor(self) -> None:
    def create_raw(_text_path, raw_path, _voice, **_kwargs):
        Path(raw_path).write_bytes(b"raw")

    def create_master(command: list[str], **_kwargs: object):
        Path(command[-1]).write_bytes(b"master")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with patch(
        "app_core.narration_service._list_macos_voices",
        return_value=(SystemVoice("Test Chinese", "zh-CN"),),
    ), patch(
        "app_core.narration_service._synthesize_macos_raw",
        side_effect=create_raw,
    ), patch(
        "app_core.narration_service.probe_audio_readback",
        side_effect=(
            AudioReadback(3_200, "1" * 64),
            AudioReadback(5_000, "2" * 64),
        ),
    ):
        artifact = synthesize_system_narration(
            "短文案",
            self.root / "narration",
            runtime=self.runtime,
            platform_name="darwin",
            runner=create_master,
        )
    self.assertEqual(artifact.speech_duration_ms, 3_200)
    self.assertEqual(artifact.master_duration_ms, 5_000)
    self.assertEqual(artifact.stream_sha256, "2" * 64)
    self.assertTrue(artifact.master_path.is_file())

def test_zero_or_over_180_second_narration_fails_before_normalization(self) -> None:
    def create_raw(_text_path, raw_path, _voice, **_kwargs):
        Path(raw_path).write_bytes(b"raw")

    def normalization_must_not_run(*_args, **_kwargs):
        raise AssertionError("无效配音时长不应启动 FFmpeg 规范化")

    for speech_duration_ms in (0, 179_800):
        with self.subTest(speech_duration_ms=speech_duration_ms), patch(
            "app_core.narration_service._list_macos_voices",
            return_value=(SystemVoice("Test Chinese", "zh-CN"),),
        ), patch(
            "app_core.narration_service._synthesize_macos_raw",
            side_effect=create_raw,
        ), patch(
            "app_core.narration_service.probe_audio_readback",
            return_value=AudioReadback(speech_duration_ms, "1" * 64),
        ), self.assertRaises(MontageFailure) as caught:
            synthesize_system_narration(
                "测试文案",
                self.root / f"invalid-{speech_duration_ms}",
                runtime=self.runtime,
                platform_name="darwin",
                runner=normalization_must_not_run,
            )
        self.assertEqual(caught.exception.code, "montage_narration_duration_invalid")

def test_unsupported_platform_has_stable_error(self) -> None:
    with self.assertRaises(MontageFailure) as caught:
        synthesize_system_narration(
            "测试",
            self.root / "unsupported",
            runtime=self.runtime,
            platform_name="linux",
        )
    self.assertEqual(caught.exception.code, "montage_narration_platform_unsupported")

def test_raw_synthesis_nonzero_exit_has_stable_error(self) -> None:
    text_path = self.root / "narration.txt"
    text_path.write_text("测试", encoding="utf-8")
    output_path = self.root / "raw.aiff"

    def failed_runner(command: list[str], **_kwargs: object):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="say failed")

    with self.assertRaises(MontageFailure) as caught:
        _synthesize_macos_raw(
            text_path,
            output_path,
            SystemVoice("Test Chinese", "zh-CN"),
            runner=failed_runner,
        )
    self.assertEqual(caught.exception.code, "montage_narration_synthesis_failed")

def test_audio_without_readable_stream_has_stable_error(self) -> None:
    audio_path = self.root / "broken.m4a"
    audio_path.write_bytes(b"broken")

    def no_stream_runner(command: list[str], **_kwargs: object):
        return subprocess.CompletedProcess(
            command, 0, stdout='{"streams": [], "format": {}}', stderr=""
        )

    with self.assertRaises(MontageFailure) as caught:
        probe_audio_readback(audio_path, runtime=self.runtime, runner=no_stream_runner)
    self.assertEqual(caught.exception.code, "montage_narration_audio_readback_failed")
```

- [ ] **Step 6: 实现统一音频回读和主音轨生成**

`probe_audio_readback()` 使用 FFprobe 的第一条音频流时长，缺失时才回退容器时长；再用 FFmpeg 流复制计算音频流哈希：

```python
hash_command = [
    str(runtime.ffmpeg), "-hide_banner", "-loglevel", "error",
    "-i", str(path), "-map", "0:a:0", "-c:a", "copy",
    "-f", "hash", "-hash", "sha256", "-",
]
```

规范化命令固定为 48kHz、双声道 AAC，并在末尾补齐静音：

```python
master_duration_ms = max(5_000, speech.duration_ms + 300)
if master_duration_ms > 180_000:
    raise MontageFailure("montage_narration_duration_invalid", "配音超过 180 秒上限")

seconds = f"{master_duration_ms / 1000:.3f}"
normalize_command = [
    str(runtime.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
    "-i", str(raw_path),
    "-af", f"aresample=48000,apad,atrim=duration={seconds}",
    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
    str(staged_master),
]
```

`probe_audio_readback()` 的启动失败、非零退出、无音轨、JSON 不可解析或无法生成哈希统一转换为 `montage_narration_audio_readback_failed`；若音轨存在但原始讲话时长小于等于 0，则由 `synthesize_system_narration()` 转换为 `montage_narration_duration_invalid`。只有规范化文件的时长误差不超过 100ms、音频流哈希可读时才原子移动为 `narration/master.m4a`；`NarrationArtifact.master_duration_ms` 保存公式得到的有效时长，FFprobe 实测值只用于误差校验。`synthesize_system_narration()` 用 `TemporaryDirectory` 保存文案、脚本、AIFF/WAV 和中间文件，最终只保留主音轨。

- [ ] **Step 7: 运行配音服务测试并提交**

Run: `.venv/bin/python -m unittest -v test_narration_service`

Expected: PASS；测试输出和 fake runner 的 command 均不包含配音原文。

```bash
git add app_core/narration_service.py test_narration_service.py
git diff --cached --check
git commit -m "feat: add offline system narration service"
```

### Task 3: 让渲染器复用批次主音轨

**Files:**
- Modify: `app_core/montage_runtime.py:249-465`
- Modify: `test_montage_runtime.py:75-240`

**Interfaces:**
- Consumes: `NarrationArtifact`、`probe_audio_readback()`
- Produces: `render_montage(..., narration: NarrationArtifact | None = None) -> dict[str, object]`

- [ ] **Step 1: 写配音切片静音和最终音轨一致性失败测试**

```python
from app_core.narration_service import AudioReadback, NarrationArtifact

def test_narration_segments_always_drop_source_audio(self) -> None:
    command = _segment_command(
        self.runtime,
        source_path=self.root / "spoken.mp4",
        start_ms=0,
        duration_ms=1_000,
        source_has_audio=True,
        audio_mode="narration",
        output_path=self.root / "segment.mp4",
    )
    self.assertIn("-an", command)
    self.assertNotIn("-filter_complex", command)

def test_narration_render_requires_matching_audio_stream_hash(self) -> None:
    source = self.root / "source.mp4"
    source.write_bytes(b"source")
    asset = VideoAsset(
        local_path=source.resolve(), sha256="a" * 64, duration_ms=10_000,
        width=1920, height=1080, fps=30, has_audio=True,
    )
    request = MontageRequest.from_mapping(
        {
            "source_paths": [str(source)], "output_count": 1,
            "target_duration_ms": 5_000, "clip_duration_ms": 1_000,
            "allow_reuse": False, "audio_mode": "narration",
            "source_audio_confirmed": False, "narration_text": "测试解说",
            "seed": 17, "title_template": "", "body_template": "",
        }
    )
    plan = plan_montages(request, (asset,))[0]
    master = self.root / "master.m4a"
    master.write_bytes(b"master")
    artifact = NarrationArtifact(
        master_path=master, voice_id="Test Chinese", voice_locale="zh-CN",
        speech_duration_ms=4_700, master_duration_ms=5_000,
        sha256="c" * 64, stream_sha256="d" * 64,
    )
    measured = VideoAsset(
        local_path=self.root / "staged.mp4", sha256="e" * 64,
        duration_ms=5_000, width=1080, height=1920, fps=30, has_audio=True,
    )

    def create_output(command: list[str], **_kwargs: object) -> None:
        target = Path(command[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"rendered")

    output = self.root / "output.mp4"
    with patch(
        "app_core.montage_runtime._run_render_command",
        side_effect=create_output,
    ), patch(
        "app_core.montage_runtime.probe_video",
        return_value=measured,
    ), patch(
        "app_core.montage_runtime.probe_audio_readback",
        return_value=AudioReadback(5_000, "f" * 64),
    ), self.assertRaises(MontageFailure) as caught:
        render_montage(
            plan, output, runtime=self.runtime,
            audio_mode="narration", narration=artifact,
        )
    self.assertEqual(caught.exception.code, "montage_narration_audio_readback_failed")
```

- [ ] **Step 2: 运行新增渲染测试并确认 `narration` 尚未支持**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_montage_runtime.MontageRuntimeTests.test_narration_segments_always_drop_source_audio \
  test_montage_runtime.MontageRuntimeTests.test_narration_render_requires_matching_audio_stream_hash
```

Expected: FAIL；`_segment_command` 进入原声分支，`render_montage` 拒绝该声音模式或参数。

- [ ] **Step 3: 扩展渲染接口并增加主音轨合并命令**

```python
def render_montage(
    plan: MontagePlan,
    output_path: Path,
    *,
    runtime: MontageRuntime,
    audio_mode: str,
    narration: NarrationArtifact | None = None,
    runner: Runner = subprocess.run,
    progress: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    if audio_mode not in {"mute", "source", "narration"}:
        raise MontageFailure("montage_audio_mode_invalid", "音频模式无效")
    if audio_mode == "narration" and narration is None:
        raise MontageFailure("montage_narration_audio_readback_failed", "本批次缺少配音主音轨")
```

`_segment_command()` 对 `{"mute", "narration"}` 都使用现有 `-an` 分支。配音模式先把切片合并到 `silent-video.mp4`，再执行：

```python
mux_command = [
    str(runtime.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
    "-i", str(silent_video), "-i", str(narration.master_path),
    "-map", "0:v:0", "-map", "1:a:0",
    "-c:v", "copy", "-c:a", "copy",
    "-movflags", "+faststart", str(staged_output),
]
```

静音和环境原声模式继续沿用现有合并路径。配音合并命令不得使用 `-t`、`-shortest` 或音频重编码：静音画面与主音轨已按同一有效时长生成，完整流复制才能保证成片音频流哈希与主音轨一致；最终时长仍由下方回读误差门验证。

- [ ] **Step 4: 实现配音成片回读和返回字段**

```python
if audio_mode == "narration":
    assert narration is not None
    audio = probe_audio_readback(staged_output, runtime=runtime, runner=runner)
    if abs(audio.duration_ms - plan.total_duration_ms) > 500:
        problems.append(f"配音时长为 {audio.duration_ms / 1000:.2f} 秒")
    if audio.stream_sha256 != narration.stream_sha256:
        problems.append("配音音轨内容与批次主音轨不一致")
    if problems:
        raise MontageFailure(
            "montage_narration_audio_readback_failed",
            "配音成片回读未通过：" + "；".join(problems),
            details={"problems": problems},
        )
```

成功返回值增加：

```python
"narration_sha256": narration.sha256 if narration else None,
"narration_stream_sha256": narration.stream_sha256 if narration else None,
"audio_stream_sha256": audio.stream_sha256 if audio_mode == "narration" else None,
```

- [ ] **Step 5: 运行运行时测试并提交**

Run: `.venv/bin/python -m unittest -v test_montage_runtime`

Expected: PASS；原静音和环境原声真实 FFmpeg 测试继续通过。

```bash
git add app_core/montage_runtime.py test_montage_runtime.py
git diff --cached --check
git commit -m "feat: mux shared narration into montage outputs"
```

### Task 4: 把配音阶段接入批次编排和原子回执

**Files:**
- Modify: `app_core/montage_service.py:6-261`
- Modify: `test_montage_service.py:16-158`

**Interfaces:**
- Consumes: `synthesize_system_narration(text, output_dir, *, runtime) -> NarrationArtifact`
- Produces: `run_montage_batch(..., narrator=synthesize_system_narration) -> MontageBatchResult`；`MontageBatchResult.narration: dict[str, object] | None`；`narration/receipt.json`

- [ ] **Step 1: 写“一次合成、时长驱动、同一主音轨复用”失败测试**

```python
def narration_artifact(self) -> NarrationArtifact:
    master = self.root / "master.m4a"
    master.write_bytes(b"audio")
    return NarrationArtifact(
        master_path=master,
        voice_id="Test Chinese",
        voice_locale="zh-CN",
        speech_duration_ms=5_000,
        master_duration_ms=5_300,
        sha256="a" * 64,
        stream_sha256="b" * 64,
    )

def test_narration_is_synthesized_once_before_planning_and_shared(self) -> None:
    artifact = self.narration_artifact()
    narration_calls: list[str] = []
    rendered: list[tuple[int, int, object]] = []

    def narrator(text, output_dir, **_kwargs):
        narration_calls.append(text)
        return artifact

    def renderer(plan, output_path, *, narration, **_kwargs):
        rendered.append((plan.index, plan.total_duration_ms, narration))
        return self.fake_renderer(plan, output_path)

    result = run_montage_batch(
        self.request(audio_mode="narration", narration_text="完整解说"),
        output_root=self.output_root,
        runtime=self.runtime,
        batch_id="M-NARRATION",
        probe=self.fake_probe,
        narrator=narrator,
        renderer=renderer,
    )
    self.assertEqual(narration_calls, ["完整解说"])
    self.assertEqual([item[1] for item in rendered], [5_300, 5_300])
    self.assertTrue(all(item[2] is artifact for item in rendered))
    self.assertEqual(result.narration["sha256"], "a" * 64)

def test_one_narration_render_failure_continues_with_later_outputs(self) -> None:
    artifact = self.narration_artifact()
    calls: list[int] = []

    def renderer(plan, output_path, **kwargs):
        calls.append(plan.index)
        if plan.index == 1:
            raise MontageFailure("montage_narration_audio_readback_failed", "第一条音轨回读失败")
        return self.fake_renderer(plan, output_path, **kwargs)

    result = run_montage_batch(
        self.request(audio_mode="narration", narration_text="完整解说"),
        output_root=self.output_root,
        runtime=self.runtime,
        batch_id="M-NARRATION-PARTIAL",
        probe=self.fake_probe,
        narrator=lambda *_args, **_kwargs: artifact,
        renderer=renderer,
    )
    self.assertEqual(calls, [1, 2])
    self.assertEqual(result.status, "partial_failure")
    self.assertEqual([item["status"] for item in result.outputs], ["failed", "success"])
```

- [ ] **Step 2: 写配音前失败也形成双回执的测试**

```python
def test_narration_failure_writes_narration_and_batch_terminal_receipts(self) -> None:
    def narrator(*_args, **_kwargs):
        raise MontageFailure("montage_narration_voice_unavailable", "没有中文声音")

    with self.assertRaises(MontageFailure):
        run_montage_batch(
            self.request(audio_mode="narration", narration_text="解说"),
            output_root=self.output_root,
            runtime=self.runtime,
            batch_id="M-NARRATION-FAILED",
            probe=self.fake_probe,
            narrator=narrator,
            renderer=self.fake_renderer,
        )

    failed_dir = self.output_root / "M-NARRATION-FAILED"
    narration_receipt = json.loads(
        (failed_dir / "narration" / "receipt.json").read_text(encoding="utf-8")
    )
    batch_receipt = json.loads(
        (failed_dir / "batch-receipt.json").read_text(encoding="utf-8")
    )
    self.assertEqual(narration_receipt["error_code"], "montage_narration_voice_unavailable")
    self.assertEqual(batch_receipt["status"], "failed")
    self.assertNotIn("running", batch_receipt.values())
```

- [ ] **Step 3: 运行批次新增测试并确认注入点不存在**

Run:

```bash
.venv/bin/python -m unittest -v \
  test_montage_service.MontageServiceTests.test_narration_is_synthesized_once_before_planning_and_shared \
  test_montage_service.MontageServiceTests.test_one_narration_render_failure_continues_with_later_outputs \
  test_montage_service.MontageServiceTests.test_narration_failure_writes_narration_and_batch_terminal_receipts
```

Expected: FAIL；`run_montage_batch()` 不接受 `narrator`，`MontageBatchResult` 没有 `narration`。

- [ ] **Step 4: 在素材探测后、镜头规划前接入配音**

```python
from dataclasses import dataclass, replace
from .narration_service import NarrationArtifact, synthesize_system_narration

@dataclass(frozen=True, slots=True)
class MontageBatchResult:
    # 保留现有字段
    narration: dict[str, object] | None

    def to_dict(self) -> dict[str, object]:
        return {
            # 保留现有字段
            "narration": self.narration,
        }

def run_montage_batch(
    request: MontageRequest,
    *,
    # 保留现有参数
    narrator: Callable[..., NarrationArtifact] = synthesize_system_narration,
    progress: Progress | None = None,
) -> MontageBatchResult:
    narration: NarrationArtifact | None = None
    # 先完成现有素材 probe。
    planning_request = request
    if request.audio_mode == "narration":
        _emit(progress, "narration_synthesizing")
        try:
            narration = narrator(
                request.narration_text,
                batch_dir / "narration",
                runtime=selected_runtime,
            )
        except Exception as exc:
            failure = exc if isinstance(exc, MontageFailure) else MontageFailure(
                "montage_narration_synthesis_failed", str(exc) or exc.__class__.__name__
            )
            _write_json_atomic(
                batch_dir / "narration" / "receipt.json",
                {"schema_version": "oneclick-montage-narration/v1", "status": "failed",
                 "error_code": failure.code, "error": str(failure), "details": failure.details},
            )
            raise failure
        _write_json_atomic(
            batch_dir / "narration" / "receipt.json",
            {"schema_version": "oneclick-montage-narration/v1", **narration.to_dict()},
        )
        _emit(progress, "narration_readback", **narration.to_dict())
        if request.clip_duration_ms > narration.master_duration_ms:
            raise MontageFailure(
                "montage_clip_duration_invalid",
                "镜头时长不能超过配音决定的成片时长",
            )
        planning_request = replace(request, target_duration_ms=narration.master_duration_ms)

    plans = plan_montages(planning_request, assets)
```

- [ ] **Step 5: 扩展渲染调用和最终回执**

```python
render_receipt = renderer(
    plan,
    output_dir / "video.mp4",
    runtime=selected_runtime,
    audio_mode=request.audio_mode,
    narration=narration,
    progress=progress,
)

narration_payload = narration.to_dict() if narration else None
receipt = {
    "schema_version": "oneclick-montage-batch-receipt/v2",
    # 保留现有字段
    "effective_target_duration_ms": planning_request.target_duration_ms,
    "narration": narration_payload,
}
```

请求落盘版本同步升级为 `oneclick-montage-request/v2`，成功与批次级失败回执都同步升级为 `oneclick-montage-batch-receipt/v2`。渲染失败项也在已存在配音时加入 `narration_sha256`；最终 `MontageBatchResult(...)` 和 `MontageBatchResult.to_dict()` 都返回同一个 `narration_payload`。批次级 `except` 必须保留当前原子 `batch-receipt.json` 写入，因此配音生成失败会同时得到配音失败回执和批次终态回执。

- [ ] **Step 6: 运行批次服务全部测试并提交**

Run: `.venv/bin/python -m unittest -v test_montage_service`

Expected: PASS；合成函数只调用一次，所有计划时长都是 5300ms，失败批次没有 pending/running。

```bash
git add app_core/montage_service.py test_montage_service.py
git diff --cached --check
git commit -m "feat: orchestrate narration before montage planning"
```

### Task 5: 在自动混剪页面增加配音文案和跟随时长状态

**Files:**
- Modify: `ui/automatic_montage_page.py:130-260,327-460`
- Modify: `test_automatic_montage_page.py:50-220`

**Interfaces:**
- Consumes: `MontageRequest.narration_text`、`MontageBatchResult.narration`、进度阶段 `narration_synthesizing/narration_readback`
- Produces: `AutomaticMontagePage.narration_text`、`narration_panel`、`target_duration_follow_label` 和三模式联动

- [ ] **Step 1: 写三模式控件、文本保留和请求映射测试**

```python
def test_narration_mode_shows_text_and_uses_follow_duration(self) -> None:
    page = self.page()
    page.source_list.item(0).setCheckState(Qt.CheckState.Checked)
    narration_index = page.audio_mode.findData("narration")
    self.assertGreaterEqual(narration_index, 0)
    page.audio_mode.setCurrentIndex(narration_index)

    self.assertFalse(page.narration_panel.isHidden())
    self.assertTrue(page.target_duration.isHidden())
    self.assertFalse(page.target_duration_follow_label.isHidden())
    self.assertTrue(page.source_audio_confirmation.isHidden())

    page.narration_text.setPlainText("  同一条解说  ")
    request = page.build_request()
    self.assertEqual(request.audio_mode, "narration")
    self.assertEqual(request.narration_text, "同一条解说")

    page.audio_mode.setCurrentIndex(page.audio_mode.findData("mute"))
    self.assertEqual(page.narration_text.toPlainText(), "  同一条解说  ")
    self.assertTrue(page.narration_panel.isHidden())
```

- [ ] **Step 2: 写配音进度和成功摘要测试**

```python
def test_narration_progress_and_success_show_voice_and_duration(self) -> None:
    page = self.page()
    page._on_progress({"stage": "narration_synthesizing"})
    self.assertIn("系统配音", page.status_label.text())
    page._on_progress({"stage": "narration_readback", "master_duration_ms": 5300})
    self.assertIn("5.3", page.status_label.text())

    result = SimpleNamespace(
        batch_dir=self.root,
        outputs=(),
        narration={"voice_id": "Test Chinese", "master_duration_ms": 5300},
    )
    page._on_success(result)
    self.assertIn("Test Chinese", page.status_label.text())
```

- [ ] **Step 3: 运行新增页面测试并确认第三种模式和控件不存在**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v \
  test_automatic_montage_page.AutomaticMontagePageTests.test_narration_mode_shows_text_and_uses_follow_duration \
  test_automatic_montage_page.AutomaticMontagePageTests.test_narration_progress_and_success_show_voice_and_duration
```

Expected: FAIL；下拉框找不到 `narration`，页面没有 `narration_panel`。

- [ ] **Step 4: 实现配音输入区和跟随时长占位**

```python
self.target_duration_label = QLabel("每条总时长")
self.target_duration_follow_label = QLabel("跟随配音")
self.target_duration_follow_label.setProperty("role", "caption")
grid.addWidget(self.target_duration_label, 1, 0)
grid.addWidget(self.target_duration, 1, 1)
grid.addWidget(self.target_duration_follow_label, 1, 1)

self.audio_mode.addItem("系统自动配音", "narration")

self.narration_panel = QFrame()
narration_layout = QVBoxLayout(self.narration_panel)
narration_layout.setContentsMargins(0, 0, 0, 0)
narration_layout.addWidget(QLabel("配音文案"))
self.narration_text = ImeAwarePlainTextEdit()
self.narration_text.setPlaceholderText("输入需要完整朗读的中文解说；整批视频共用这一条配音")
self.narration_text.setMaximumHeight(120)
narration_layout.addWidget(self.narration_text)
narration_hint = QLabel("系统会移除素材原声，视频时长由配音决定。")
narration_hint.setProperty("role", "caption")
narration_layout.addWidget(narration_hint)
layout.addWidget(self.narration_panel)
```

- [ ] **Step 5: 实现三模式联动、请求字段和进度反馈**

```python
def _sync_audio_mode_controls(self, _index: int | None = None) -> None:
    mode = self.audio_mode.currentData()
    source_mode = mode == "source"
    narration_mode = mode == "narration"
    self.source_audio_confirmation.setVisible(source_mode)
    if not source_mode:
        self.source_audio_confirmation.setChecked(False)
    self.narration_panel.setVisible(narration_mode)
    self.target_duration.setVisible(not narration_mode)
    self.target_duration_follow_label.setVisible(narration_mode)

# build_request() 增加：
"narration_text": self.narration_text.toPlainText(),

# _on_progress() 增加：
elif stage == "narration_synthesizing":
    self.progress_bar.setValue(16)
    self.status_label.setText("正在生成系统配音…")
elif stage == "narration_readback":
    duration = int(event.get("master_duration_ms") or 0) / 1000
    self.progress_bar.setValue(22)
    self.status_label.setText(f"配音已生成，成片时长 {duration:g} 秒；正在安排镜头…")
```

`_on_success()` 用 `getattr(result, "narration", None)` 兼容旧测试替身；有配音时显示声音名称和主音轨时长，无配音时保留原成功文案。

- [ ] **Step 6: 运行页面和请求模块测试并提交**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v \
  test_automatic_montage_page test_montage_models
```

Expected: PASS；中文输入法控件仍使用 `ImeAwarePlainTextEdit`，切换模式不丢文案。

```bash
git add ui/automatic_montage_page.py test_automatic_montage_page.py
git diff --cached --check
git commit -m "feat: add narration controls to montage page"
```

### Task 6: 建立当前 Mac 的真实端到端配音测试

**Files:**
- Create: `test_montage_narration_integration.py`
- Test: `test_narration_service.py`
- Test: `test_montage_narration_integration.py`

**Interfaces:**
- Consumes: `synthesize_system_narration()`、`run_montage_batch()`、系统 `/usr/bin/say` 与完整 FFmpeg
- Produces: 三条真实 MP4、同一主音轨哈希及无素材原声的机器回读证据

- [ ] **Step 1: 写只在 macOS 与完整运行时存在时执行的真实测试**

```python
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from app_core.montage_models import MontageRequest
from app_core.montage_runtime import MontageRuntime
from app_core.montage_service import run_montage_batch
from app_core.narration_service import probe_audio_readback

class MontageNarrationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @unittest.skipUnless(
        sys.platform == "darwin"
        and Path("/usr/bin/say").is_file()
        and shutil.which("ffmpeg")
        and shutil.which("ffprobe"),
        "requires macOS say and complete FFmpeg",
    )
    def test_real_batch_reuses_one_system_narration_for_three_outputs(self) -> None:
        ffmpeg = Path(shutil.which("ffmpeg") or "")
        ffprobe = Path(shutil.which("ffprobe") or "")
        runtime = MontageRuntime(ffmpeg=ffmpeg, ffprobe=ffprobe)
        source_a = self.root / "red-440hz.mp4"
        source_b = self.root / "blue-880hz.mp4"
        for color, frequency, target in (
            ("red", 440, source_a),
            ("blue", 880, source_b),
        ):
            subprocess.run(
                [
                    str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c={color}:s=320x180:r=30",
                    "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000",
                    "-t", "20", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(target),
                ],
                check=True,
            )
        source_audio_hashes = {
            probe_audio_readback(path, runtime=runtime).stream_sha256
            for path in (source_a, source_b)
        }
        request = MontageRequest.from_mapping(
            {
                "source_paths": [str(source_a), str(source_b)],
                "output_count": 3,
                "target_duration_ms": 5_000,
                "clip_duration_ms": 1_000,
                "allow_reuse": False,
                "audio_mode": "narration",
                "source_audio_confirmed": False,
                "narration_text": "你好，这是一段本机自动配音测试。",
                "seed": 20260903,
                "title_template": "",
                "body_template": "",
            }
        )
        evidence_root = os.environ.get("ONECLICK_MONTAGE_EVIDENCE_DIR")
        output_root = Path(evidence_root).resolve() if evidence_root else self.root / "outputs"
        result = run_montage_batch(
            request,
            output_root=output_root,
            runtime=runtime,
            batch_id="M-NARRATION-INTEGRATION",
        )
        self.assertEqual(result.status, "success")
        self.assertEqual(len(result.outputs), 3)
        self.assertIsNotNone(result.narration)
        assert result.narration is not None
        master_hash = result.narration["stream_sha256"]
        self.assertTrue(all(item["audio_stream_sha256"] == master_hash for item in result.outputs))
        self.assertTrue(all(item["narration_sha256"] == result.narration["sha256"] for item in result.outputs))
        self.assertTrue(all(item["audio_stream_sha256"] not in source_audio_hashes for item in result.outputs))
        self.assertTrue(
            all(abs(item["duration_ms"] - item["expected_duration_ms"]) <= 500 for item in result.outputs)
        )
        receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["summary"], {"success": 3, "failed": 0})
        print(f"NARRATION_EVIDENCE={result.receipt_path}")
```

- [ ] **Step 2: 运行真实测试并让环境问题明确失败**

Run:

```bash
narration_evidence_dir="$(mktemp -d /tmp/oneclick-narration-integration.XXXXXX)"
ONECLICK_MONTAGE_EVIDENCE_DIR="$narration_evidence_dir" \
  .venv/bin/python -m unittest -v test_montage_narration_integration
jq '{status, summary, effective_target_duration_ms, narration, outputs: [.outputs[] | {status, duration_ms, expected_duration_ms, narration_sha256, audio_stream_sha256}]}' \
  "$narration_evidence_dir/M-NARRATION-INTEGRATION/batch-receipt.json"
```

Expected on current Mac: PASS。若返回 `montage_narration_voice_unavailable`，记录为当前 Mac 缺少中文声音并停止“Mac 可用”结论；不得把该项改成跳过。

- [ ] **Step 3: 检查同一命令块输出的真实批次回执，而不是只看测试进程退出码**

Expected: `status=success`、`summary.success=3`；三个 `narration_sha256` 相同，三个 `audio_stream_sha256` 与主音轨相同，每条实测时长与期望误差不超过 500ms。保留终端打印的精确证据目录直到 Task 7 状态记录完成；它不进入 Git。

- [ ] **Step 4: 提交真实集成测试**

```bash
git add test_montage_narration_integration.py test_narration_service.py
git diff --cached --check
git commit -m "test: verify real mac montage narration pipeline"
```

### Task 7: 更新项目边界并完成回归与人工听审停点

**Files:**
- Modify: `README.md:8-35`
- Modify: `QUALITY_GATES.md:121-205`
- Modify: `SOURCE_OF_TRUTH.md:7-12,69,364-366`
- Verify: `docs/architecture/automatic-montage-system-narration.architecture.html`
- Verify: `docs/architecture/automatic-montage-system-narration.lifecycle.html`

**Interfaces:**
- Consumes: Tasks 1–6 的源码、测试结果、真实 Mac 批次回执
- Produces: 当前能力说明、质量门、真实验收边界和唯一下一步

- [ ] **Step 1: 更新 README 的用户可见能力说明**

将“自动混剪”段落补充为：

```markdown
声音支持静音、经确认的环境原声和系统自动配音。系统自动配音只使用本机中文声音，同一批次只合成一次并复用到所有成片；视频时长跟随完整配音，素材原声会被移除。该能力不调用云端模型，不会自动进入发布任务。
```

- [ ] **Step 2: 把配音回读要求加入质量门**

在“本地自动混剪”增加以下精确门槛：

```markdown
9. 系统自动配音必须在镜头规划前只合成一次；所有输出引用同一主音轨文件哈希和音频流哈希，不能逐条重新合成。
10. 配音模式必须移除素材原声，以实际讲话时长加 0.3 秒尾部余量决定成片时长；超过 180 秒、缺少中文声音或音轨回读不一致时在提交发布前失败。
11. macOS 与 Windows 分别完成真实系统中文声音验收；Mac 通过不能代替 Windows 真机或安装包验收。
12. 系统配音生成成功只代表本地成片能力，不自动获得平台预检或正式发布授权。
```

- [ ] **Step 3: 更新 SOURCE_OF_TRUTH，严格区分源码、Mac 和 Windows 状态**

记录本轮实际测试数量、真实批次号、系统声音名称、三个输出哈希回读结果；Windows 保持“只有适配器合同测试，未真机验证”。唯一下一步写为“大帅在源码客户端播放一条真实素材成片，确认音色、语速和末句完整性”，不得写成已经打包或可对外交付。

- [ ] **Step 4: 运行全部定向测试**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v \
  test_montage_models test_narration_service test_montage_runtime \
  test_montage_service test_automatic_montage_page \
  test_montage_narration_integration test_main_window
```

Expected: PASS；真实 Mac 集成测试不跳过。

- [ ] **Step 5: 运行离屏客户端与完整离线回归**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python desktop_native_app.py --ui-test
.venv/bin/python -m unittest discover -p 'test_*.py'
git diff --check
```

Expected: `NATIVE_DESKTOP_UI_OK`；完整测试零失败、零错误；记录本次实际测试数和耗时，不沿用历史 `3135/3135`。

- [ ] **Step 6: 重新验证两张已批准架构图没有漂移**

Run:

```bash
node /Users/andy/.codex/skills/archify/bin/archify.mjs validate architecture \
  docs/architecture/automatic-montage-system-narration.architecture.json \
  --quality showcase --json
node /Users/andy/.codex/skills/archify/bin/archify.mjs validate lifecycle \
  docs/architecture/automatic-montage-system-narration.lifecycle.json \
  --quality showcase --json
```

Expected: 两项均为 `9/9 showcase`、`0 errors`、`0 warnings`。

- [ ] **Step 7: 提交文档和状态更新**

```bash
git add README.md QUALITY_GATES.md SOURCE_OF_TRUTH.md
git diff --cached --check
git commit -m "docs: record montage narration verification boundary"
```

- [ ] **Step 8: 打开源码客户端进入人工听审停点**

先正常退出已安装的一键发客户端，再运行：

```bash
.venv/bin/python tools/run_source_live.py --page montage
```

使用素材管理中的至少两条真实视频，输入一段短中文解说，生成三条成片。大帅双击任一结果并确认：音色可接受、语速可接受、末句完整、没有素材原声串入。该人工听审通过前，状态保持“Mac 源码机器验收通过、人工听审待确认”；不升级版本、不打包。
