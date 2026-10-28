# 智护忆途 源代码

`esp32-fall-gps/`：ESP32-WROVER固件；`backend/`：Flask后端、网页、数据库和模型；`ml/`：训练代码与离线报告。独立接线单见 `接线说明.txt`。

## 运行

1. 使用Python 3.12，安装 `pip install -r backend/requirements.txt`；安装并启动MySQL。
2. 将 `backend/.env.example` 复制为 `backend/.env`，填写本机数据库配置。
3. 执行 `python backend/app.py`，浏览器打开 `http://127.0.0.1:5000`。首次启动自动按 `schema.sql` 建表；升级既有库时另行核对 `migrations/`。
4. 地图配置：填写 `backend/templates/map.html` 的 `securityJsCode` 和 `backend/static/map.js` 的地图 `key`，使用自己的高德Web端应用配置。
5. Arduino IDE打开 `esp32-fall-gps/esp32-fall-gps.ino`，按 `arduino-libraries.txt` 安装工具链和库，选择ESP32 Wrover Module；填写 `STA_SSID`、`STA_PASS`、`AP_PASS`、`BACKEND_URL` 后上传。引脚以 `board_pins.h` 为准。

模型已包含于 `backend/models/`，无需重新训练。它在电脑后端运行，网页显示模型疑似跌倒；设备报警和拍照仍按本地规则执行。数据与照片在运行后生成，不随源码提交。

## 训练与测试

- 重新训练：安装 `ml/requirements.txt`，在项目根运行 `python ml/download_sisfall.py`，再运行 `python ml/train_sisfall.py --output .build/retrained/model.json --report .build/retrained/report.json`。原始公开训练数据不随源码提交；SisFall原论文 DOI：10.3390/s17010198。
- 后端测试：`python -m unittest discover -s backend/tests -p "test_*.py"`；固件算法测试源文件位于 `esp32-fall-gps/tests/`。
- 网页测试为可选开发依赖：`pip install -r backend/requirements-dev.txt` 和 `npm install`；分别启动 `python backend/tests/model_ui_server.py` 或 `python backend/tests/photo_ui_server.py`，再运行 `npm run test:ui:model` 或 `npm run test:ui:photos`。默认使用Windows本机Chrome；其他安装位置可设置 `CHROME_PATH`。
