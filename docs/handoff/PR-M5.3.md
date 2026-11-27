# M5.3 Streamlit 本机核对页面

从仓库根目录运行 `uv run --no-sync streamlit run src/video_agent/ui/app.py`。页面支持授权上传、输入已有资产及分析 ID、提交分析与问答、查询进度、取消、分页时间轴和查看引用。页面与 API 的连线使用服务端 Bearer 头。回答正文及缺口作为纯文本展示，每条主张带证据 ID。无效本机配置以固定提示显示。

播放映射只有在资产与受控来源匹配、首中尾校验成立且浏览器可安全访问时才产生导航秒数。当前 Bearer 源在页面显示 `preview_only`，保留实际帧、原始 PTS 和实际毫秒。F04 语音使用原轴，不会重复加上音轨偏移。页面控件与项目服务均限 64 MiB，超过时使用 CLI 导入。

| 独立冻结点 | 验收 |
| --- | --- |
| `f76bdfc` · SHA-256 `7accb722de597c44a3bd56935f5d1e08f85cb27cc20c938d7e6a395128ba5cc0` | 两个 Step 0 签名只能返回未实现；5 个行为失败。`d5b27fa` 接入证据预览与正式 API。 |
| `fd2ea08` · `de1d0035a57eda2a2586a36bae43554438c847fc3ee4a677684ca6184c8d1cf1` | 非 ASCII 令牌、非法端口可能产生未捕获异常，回答未按纯文本逐条展示。`688cb43` 修复。 |
| `327e0d3` · `9d9f4032ae36b475a5183563c15537de1c15a5ef6c14e4e503868d33fa26dc3c` | 项目未设置时框架报告 `[None, 200, True]`。配置生效后为 `["127.0.0.1", 64, false]`，隔离用户配置的子进程验证三个值都来自仓库。`ba4260e` 将这份非机密配置纳入 CI 镜像。 |

~~~powershell
uv run --no-sync pytest -q tests/unit/ui tests/e2e/test_review_ui.py tests/integration/test_pts_sampling.py --disable-socket --allow-unix-socket
~~~

真实 Uvicorn 进程与 Streamlit AppTest 覆盖提交问题到 Worker 完成，再到页面刷新、显示答案及帧。另启 Streamlit 服务，实际取得首页 HTTP 200 和健康检查 `ok`。这些检查覆盖服务及页面组件；原生浏览器的视频交互未作人工验收。[使用与预览](../runbooks/recovery.md)
