# v1.7 Test Report

## 静态检查

- Python `compileall`: passed
- JavaScript syntax: passed
- AudioWorklet syntax: passed

## Python tests

```text
20 passed
```

覆盖：

- Qwen smart_turn 必需 modalities；
- TTS 端点预检与降级；
- Qwen 模式忽略本地噪声候选；
- Qwen `speech_started` 进入可逆 Hold；
- `turn_invalid` 原位续播；
- 稳定云端 ASR 前缀确认硬打断；
- legacy hybrid/hard/semantic A/B 行为；
- ambient 有效短句提升；
- 播放结束和 TTS task-finished 分离。

## JavaScript tests

- 3.1 秒 burst PCM 完整保留；
- 首播预缓冲；
- 中途断粮重缓冲；
- source_finished 尾音播放；
- legacy Hold 240ms 滚动缓存；
- Qwen Hold 完整保存 1 秒新增音频，无 `hold_trim`；
- 跨 WebSocket 帧重采样相位连续。

## 云端测试

使用用户提供配置尝试 TTS 预检，但当前沙箱 DNS 无法解析阿里云域名，错误为：

```text
Temporary failure in name resolution
```

因此无法在此环境中完成真实音频试听。发布包不包含 `.env` 或 API Key。
