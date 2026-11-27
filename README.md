# 1688 选品找货与卖家联系方式工作台

面向亚马逊选品的本地化 1688 找同款与供应商联系方式采集工作台，基于 SQLite（`data/state.db`）与本地 Web 工作台完成 **导入选品表 → 图搜/词搜匹配同款与店铺详情 → 获取 24h 虚拟号/实号 + 旺旺半自动群发 → 导出联系方式 Excel** 全流程。

支持 **Windows 10/11** 与 **macOS（Apple Silicon M 系列 & Intel 通用）**。

---

## 前置依赖（请先安装）

1. **Python 3.10+**
   - **Windows**：从 [Python 官网](https://www.python.org/downloads/windows/) 安装，安装界面务必勾选 **`Add python.exe to PATH`**。
   - **macOS**：安装官方 Python 3.10+ 或通过 Homebrew `brew install python@3.11`。
2. **Google Chrome 浏览器**
   - 详情识别、抓取电话与旺旺聊天直接调用本机安装的 Google Chrome（无需额外下载 Chromium）。

---

## 快速构建与启动

### 方式一：一键自动建环境并启动（推荐）

克隆或下载仓库压缩包解压后：

- **macOS（M 系列 / Intel 通用）**
  - 双击 **`启动工作台.command`**（首次若系统拦截提示“无法验证开发者”，右键该文件 → 点「打开」放行一次即可；也可在终端运行 `chmod +x *.command && ./启动工作台.command`）。
  - 首次运行会自动创建 `.venv` 虚拟环境并安装依赖，完成后自动打开默认浏览器进入工作台。
  - 需要退出时：双击 **`停止工作台.command`**，或直接点击网页右上角 **「停止服务」**。

- **Windows（Win10 / Win11）**
  - 双击 **`启动工作台.bat`**。
  - 首次运行会自动创建 `.venv` 虚拟环境并安装依赖，完成后自动打开默认浏览器进入工作台。
  - 需要退出时：双击 **`停止工作台.bat`**，或直接点击网页右上角 **「停止服务」**。

---

### 方式二：手动命令行构建（Windows / macOS）

如果更习惯自己管理虚拟环境，可在项目根目录按对应系统执行：

#### macOS（M 系列 & Intel）
```bash
cd 1688-sourcing-tool
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
.venv/bin/python web_ui.py
```

#### Windows（CMD / PowerShell）
```cmd
cd 1688-sourcing-tool
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
.venv\Scripts\python web_ui.py
```

启动后默认监听 **`http://127.0.0.1:8000`**（端口被占用时会自动顺延），会自动弹出浏览器打开工作台。

---

## 首次使用引导

1. **配置个人 1688 AK（只需一次）**
   - 点击页面右上角 **「1688 AK 配置」→「一键浏览器授权获取 AK」**（或直接点「获取同款商品」按弹窗引导授权），确认授权后会自动写入本地 `config.json`（已被 `.gitignore` 忽略，不会上传仓库）。
   - 也可以参考 [config.example.json](config.example.json) 手工填写。
2. **1688 账号扫码登录（按需触发，长期免登）**
   - 首次跑同款详情补充或抓取电话时，如尚未登录 1688，Chrome 会停在登录页等待手机扫码；扫码完成后会自动继续当前任务并把登录态保存到 `browser_profile/`，后续免登（过期后再自动弹出重扫即可）。
   - 也可随时点击页面右上角 **「1688 扫码登录」** 主动登录或切换账号。

---

## 目录结构

```text
1688-sourcing-tool/
├── 启动工作台.command / 停止工作台.command   # macOS 一键启动与停止（首次自动建 .venv）
├── 启动工作台.bat / 停止工作台.bat           # Windows 一键启动与停止（首次自动建 .venv）
├── web_ui.py                               # 本地 Web 工作台后端服务
├── index.html                              # 工作台前端界面（双 Tab + 弹窗设置）
├── runtime_config.py                       # 跨平台路径探测、config.json 与进程管理
├── step1_import_excel.py                   # Step 1：导入双 Sheet Excel/CSV、下主图、译中文词
├── step2_find_matches.py                   # Step 2：多图/词搜找同款、按店去重、详情补全
├── step3_get_contacts.py                   # Step 3：提取工厂 24h 虚拟号 & 公司页实号
├── step4_wangwang_chat.py                  # Step 4：单窗口多 Tab 旺旺半自动填入话术
├── skills/                                 # 内置 1688-product-find & 1688-source-suppliers
├── config.example.json                     # 本地配置结构示例（实际 config.json 不入库）
└── requirements.txt                        # Python 依赖清单
```

*(运行后会在本地自动生成 `data/`、`image/`、`browser_profile/`、`config.json`、`.venv/`，均已在 `.gitignore` 中排除。)*

---

## 工作台功能与注意事项

1. **原始商品库（Tab 1）**：支持双 Sheet Excel/CSV 导入、主图落盘（`image/<ASIN>.jpg`）与附图链接保存、自动翻译中文关键词；支持多图勾选/词搜找同款及详情补充（双段进度条 + 一键中断）；支持物理删除商品（保留同款）。
2. **1688 同款与卖家联系方式（Tab 2）**：按 ASIN 分页默认折叠展示，支持单行/批量抓取「工厂 24h 虚拟号」与「公司页实号」、实号/联系人/跟进状态/多行备注失焦即存、单窗口多 Tab 旺旺半自动填入话术、物理删除同款，以及顶部随时导出 `3_seller_contacts.xlsx`。
3. **跨平台与网络说明**：
   - Windows 终端与子进程已强制使用 UTF-8 编码，避免中文与状态图标在 GBK 环境下报错；
   - 导入阶段英文翻译默认优先尝试 Google Translate（超时 2.5s 自动降级到 MyMemory），无代理环境也能顺畅完成导入；
   - 虚拟号 24 小时过期，联系后可将老板真实手机号直接补录在实号输入框中。
