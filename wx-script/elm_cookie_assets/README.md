# AWSC 登录资源

本目录供同级 `elm_cookie.py` 的 `build_login_context()` 调用，不是青龙定时任务目录。

- `awsc_bridge.cjs`：Python 与 Node 之间的文件协议桥。
- `awsc_params.cjs`：加载 SDK，组装登录辅助参数。
- `mor.v.js`、`plugin_/`：用户提供的淘宝闪购参考资源中的 SDK 文件。
- `@babel/`：上述 SDK 使用的运行时辅助模块。
- `package.json`：明确本目录按 CommonJS 加载，避免继承父目录的模块类型。

本次补齐保留所提供资源的源文件内容及现有标识，不将第三方 SDK 宣称为仓库作者的原创实现。资源版本、接入方式和业务接口可能变化；完整资源能加载不代表真实账号一定登录成功。

不要提交 `.protocol_device.json`、`algorithm_params_result.json`、账号会话、Cookie 或运行时输出。本仓库只包含源文件，不包含已运行设备的缓存。

目录名称固定为 `elm_cookie_assets`，不要替换为 `eleme_assets`。青龙需要通过依赖字段同步 `.js`、`.cjs`、`.json` 文件及其原有目录结构，见 [使用说明](../../docs/elm_cookie.md)。
