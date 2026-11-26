# M3.3 观察融合与来源

原始 ASR、未叠加网格的父帧、实际送入的帧或裁剪、窗口观察和证据边均在磁盘 SQLite 保留。音轨内容记作 heard，可见文字记作 observed，跨帧推断保留 inferred 和它引用的证据。冲突数值作为独立观察保存。模型、revision、Prompt bundle、输出 Schema、采样、profile、crop 与输入摘要随观察记录。同一证据 ID 绑定原内容。[M3.2](PR-M3.2.md)

签名提交 `3981d2a`。两项磁盘用例先在占位实现上退出 1，测试冻结于 `ab23161`，diff SHA-256 为 `e2896df5e498b2931e3a52fd2594cb6b2bb293e76624de663fb3b7df43908d2b`，实现为 `67a344e`。独立复核发现无提示的 crop 缺父证据、多父帧提示可随列表顺序选错，追加两项红灯于 `99a27d1`，diff SHA-256 为 `bf68afb515ad43331d7ee9d6bc88a618c1545364a28aaccd3a1029c5d9fc85a3`，修复为 `571892f`。

```powershell
uv run --no-sync pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket tests/integration/test_observation_pipeline.py tests/integration/test_observation_parent.py
```

4 项通过。图像和语音的实际识别效果另由真实评测判断。窗口提交和复用在下一阶段接入。[M3.4](PR-M3.4.md)
