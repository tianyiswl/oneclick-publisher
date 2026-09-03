# 系统自动配音最终集中修复报告

日期：2026-09-03

## 状态

两项 Important 已完成源码修复和自动化回归：

1. 中文声线发现不再限定四个地区码，而是接受规范化后主语言为 `zh` 的 BCP-47 风格 locale；`Hans` 或简体中文地区优先，其余中文按规范化 locale、voice id 稳定排序，非中文声音不会成为兜底。
2. Windows 声线枚举在 PowerShell 边界强制把零个、单个、多个声音序列化为 JSON 数组；Python 解析器同时兼容 PowerShell 仍返回单对象的情况。

本轮没有修改版本、打包、发布、平台、当前源码客户端或 QuickTime 试听停点，也没有执行任何真实平台动作。

## 改动

### `app_core/narration_service.py`

- 增加 BCP-47 风格 locale 校验、规范化和显示用标准大小写转换。
- 以规范化 locale 的主语言是否等于 `zh` 判断中文，覆盖 `zh-Hans`、`zh-Hant`、`zh-MO` 及同类中文 locale。
- `Hans` 和简体地区排在其他中文之前；同层按 locale、voice id 确定性排序。
- macOS 声音列表解析从“两字母语言 + 两字母地区”扩展为带脚本、地区和后续子标签的 BCP-47 风格 locale。
- Windows PowerShell 先保存 `$voices = @(...)`，再使用 `ConvertTo-Json -InputObject $voices -Compress`，避免零个或单个声音时丢失数组外形。
- Windows Python 解析器把 JSON 对象规范化为单元素列表后复用原列表解析逻辑。

### `test_narration_service.py`

- 新增 `zh-Hans` 简体优先测试。
- 新增 `zh-MO` 在混合语言集合中仍可选测试。
- 新增 `zh-Hant` 的 locale、voice id 稳定排序和英语、日语排除测试。
- 新增 macOS `zh_Hans`、`zh_MO` 解析测试。
- 新增 Windows 零个、单个、多个声音 JSON 形态测试。
- 新增 Windows PowerShell 枚举命令强制 JSON 数组边界测试。

## TDD 证据

### RED

先只修改测试，未修改生产代码，运行：

```text
.venv/bin/python -m unittest -v \
  test_narration_service.NarrationServiceTests.test_choose_chinese_voice_prefers_hans_locale \
  test_narration_service.NarrationServiceTests.test_choose_chinese_voice_accepts_macao_locale \
  test_narration_service.NarrationServiceTests.test_choose_chinese_voice_stably_sorts_mixed_language_voices \
  test_narration_service.NarrationServiceTests.test_macos_voice_list_accepts_script_and_macao_locales \
  test_narration_service.NarrationServiceTests.test_windows_voice_parser_accepts_zero_voices \
  test_narration_service.NarrationServiceTests.test_windows_voice_parser_normalizes_one_voice_object \
  test_narration_service.NarrationServiceTests.test_windows_voice_parser_accepts_multiple_voices \
  test_narration_service.NarrationServiceTests.test_windows_voice_discovery_forces_json_array_at_powershell_boundary
```

输出摘要：

```text
Ran 8 tests in 0.002s
FAILED (failures=3, errors=3)
```

失败原因与待修问题一致：旧选择器拒绝 `zh-Hans`、`zh-MO`、`zh-Hant`；旧 macOS 正则漏掉脚本子标签；旧 Windows 解析器丢弃单对象；旧 PowerShell 脚本未通过 `-InputObject` 固定数组边界。零个和多个声音是已有兼容行为，在 RED 中继续通过。

### GREEN

实现最小修复后重跑同一命令，输出：

```text
Ran 8 tests in 0.002s
OK
```

## 验证命令与输出

配音服务定向测试：

```text
.venv/bin/python -m unittest -v test_narration_service

Ran 26 tests in 0.011s
OK
```

配音及受影响混剪回归：

```text
.venv/bin/python -m unittest -v \
  test_narration_service \
  test_montage_models \
  test_montage_runtime \
  test_montage_service \
  test_automatic_montage_page

Ran 76 tests in 1.579s
OK
```

界面测试输出包含 Qt 对缺失 `Sans Serif` 字体别名的性能提示；测试没有失败，该提示与本轮配音修改无关。

静态与范围检查：

```text
.venv/bin/python -m py_compile app_core/narration_service.py test_narration_service.py
exit 0

git diff --check -- app_core/narration_service.py test_narration_service.py
exit 0

.venv/bin/python tools/check_workstream_scope.py --stream integration --base HEAD
scope-check: OK
```

## 自审

- 需求覆盖：`zh-Hans`、`zh-Hant`、`zh-MO` 和混合语言已覆盖；英语仍由既有“无中文声音”测试证明不会兜底。
- Windows 数量边界：零个、单个、多个 JSON 均有独立测试；PowerShell 强制数组和 Python 单对象容错各有测试。
- 变更范围：功能代码只改 `app_core/narration_service.py`，测试只改 `test_narration_service.py`；本报告是任务明确要求的附加产物。
- 工作树隔离：开始时已有其他未提交、未跟踪改动，均保留原样，不纳入本轮暂存或提交。
- 未验证边界：当前仍未在 Windows 真机运行 PowerShell/System.Speech 或打包客户端；不能据本轮自动化测试宣称 Windows 真实配音可用。
