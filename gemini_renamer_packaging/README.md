# Gemini File Renamer - 打包指南

将 Python GUI 程序打包为 macOS DMG 和 Windows EXE 的完整指南。

## 📁 文件结构

```
gemini_renamer_packaging/
├── gemini_file_renamer_gui.py  # 你的主程序（需要复制进来）
├── build_app.py                 # 自动化构建脚本
├── requirements.txt             # Python 依赖
├── icon.ico                     # Windows 图标（可选）
├── icon.icns                    # macOS 图标（可选）
└── .github/
    └── workflows/
        └── build.yml            # GitHub Actions 自动构建
```

## 🚀 快速开始

### 方法一：本地构建（推荐）

#### Windows 上构建 EXE

```powershell
# 1. 安装依赖
pip install -r requirements.txt

# 2. 运行构建脚本
python build_app.py

# 3. 输出位置
# dist/GeminiRenamer.exe
```

#### macOS 上构建 DMG

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 运行构建脚本
python build_app.py

# 3. 输出位置
# dist/GeminiRenamer.dmg
```

### 方法二：使用 GitHub Actions（跨平台自动构建）

1. 将代码推送到 GitHub 仓库
2. 确保 `.github/workflows/build.yml` 存在
3. 创建一个 tag 触发构建：
   ```bash
   git tag v1.0.0
   git push origin v1.0.0
   ```
4. 在 GitHub Actions 页面查看构建进度
5. 构建完成后在 Releases 页面下载

## 📋 手动构建命令

如果自动脚本不工作，可以手动执行：

### Windows

```powershell
pyinstaller --onefile --windowed --name=GeminiRenamer `
    --hidden-import=google.generativeai `
    --hidden-import=customtkinter `
    --hidden-import=pymupdf `
    --hidden-import=fitz `
    --hidden-import=docx `
    --hidden-import=ebooklib `
    --hidden-import=bs4 `
    --hidden-import=pathvalidate `
    --collect-submodules=google.generativeai `
    --collect-submodules=customtkinter `
    --collect-submodules=ebooklib `
    gemini_file_renamer_gui.py
```

### macOS

```bash
# 步骤 1: 构建 .app
pyinstaller --onefile --windowed --name=GeminiRenamer \
    --osx-bundle-identifier=com.gemini.renamer \
    --hidden-import=google.generativeai \
    --hidden-import=customtkinter \
    --hidden-import=pymupdf \
    --hidden-import=fitz \
    --hidden-import=docx \
    --hidden-import=ebooklib \
    --hidden-import=bs4 \
    --hidden-import=pathvalidate \
    --collect-submodules=google.generativeai \
    --collect-submodules=customtkinter \
    --collect-submodules=ebooklib \
    gemini_file_renamer_gui.py

# 步骤 2: 创建 DMG
hdiutil create -volname "Gemini File Renamer" \
    -srcfolder dist/GeminiRenamer.app \
    -ov -format UDZO \
    dist/GeminiRenamer.dmg
```

## 🎨 添加应用图标

### 制作图标文件

1. 准备一张 1024×1024 的 PNG 图片

2. **Windows 图标 (.ico)**：
   - 使用在线工具如 [ConvertICO](https://convertico.com/)
   - 或使用 ImageMagick：
     ```bash
     magick convert icon.png -define icon:auto-resize=256,128,64,48,32,16 icon.ico
     ```

3. **macOS 图标 (.icns)**：
   ```bash
   # 创建 iconset 文件夹
   mkdir icon.iconset
   sips -z 16 16 icon.png --out icon.iconset/icon_16x16.png
   sips -z 32 32 icon.png --out icon.iconset/icon_16x16@2x.png
   sips -z 32 32 icon.png --out icon.iconset/icon_32x32.png
   sips -z 64 64 icon.png --out icon.iconset/icon_32x32@2x.png
   sips -z 128 128 icon.png --out icon.iconset/icon_128x128.png
   sips -z 256 256 icon.png --out icon.iconset/icon_128x128@2x.png
   sips -z 256 256 icon.png --out icon.iconset/icon_256x256.png
   sips -z 512 512 icon.png --out icon.iconset/icon_256x256@2x.png
   sips -z 512 512 icon.png --out icon.iconset/icon_512x512.png
   sips -z 1024 1024 icon.png --out icon.iconset/icon_512x512@2x.png
   iconutil -c icns icon.iconset
   ```

## 🔧 常见问题

### 1. "Module not found" 错误

添加缺失的模块到 `--hidden-import`：
```bash
--hidden-import=缺失的模块名
```

### 2. 打包后程序无法启动

- 先用 `--console` 替换 `--windowed` 调试
- 查看控制台输出的错误信息

### 3. 文件体积太大

添加排除项减小体积：
```bash
--exclude-module=matplotlib
--exclude-module=numpy
--exclude-module=pandas
--exclude-module=scipy
```

### 4. macOS 提示"无法验证开发者"

```bash
# 方法 1: 右键点击应用 → 打开
# 方法 2: 系统偏好设置 → 安全性与隐私 → 仍要打开
# 方法 3: 命令行移除隔离属性
xattr -cr /Applications/GeminiRenamer.app
```

### 5. Windows 杀毒软件报警

这是 PyInstaller 打包程序的常见问题，可以：
- 在杀毒软件中添加白名单
- 使用代码签名证书签名 EXE

## 📦 创建 Windows 安装程序（可选）

使用 [Inno Setup](https://jrsoftware.org/isinfo.php) 创建专业的安装程序：

1. 下载安装 Inno Setup
2. 运行 `build_app.py` 会生成 `.iss` 脚本
3. 在 Inno Setup 中打开并编译

## ⚠️ 重要提示

- **必须在目标平台上构建**：Windows EXE 需在 Windows 上构建，DMG 需在 macOS 上构建
- **Python 版本**：推荐使用 Python 3.9-3.11
- **测试**：打包后务必在干净的系统上测试

## 📄 许可证

MIT License
