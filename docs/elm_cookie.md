# 淘宝闪购 Cookie 同步

脚本：`wx-script/elm_cookie.py`，任务名称保留为 `elm_cookie`。默认定时：`15 6,20 * * *`。

## 资源位置与青龙订阅

`elm_cookie_assets` 必须与 `elm_cookie.py` 同级，文件夹名称不能写成 `eleme_assets`。仓库包含 40 个资源源文件：Node 参数桥、参数组装模块、SDK 和 Babel 辅助模块，无须另行安装 npm 包。SDK 资源不是独立任务。

只拉微信小程序脚本：

```bash
ql repo "https://github.com/lcmovie/YYB-GO-Script-i.git" "^wx-script/[^/]+\.(js|py)$" "" "^wx-script/elm_cookie_assets/.*\.(js|cjs|json)$" "main" "js py cjs json"
```

在青龙“订阅管理”中，已有订阅应同步更新以下字段；不要新建重复订阅：

| 字段 | 内容 |
|---|---|
| 白名单（只拉微信脚本） | `^wx-script/[^/]+\.(js\|py)$` |
| 白名单（拉全部脚本） | `^(wx-script\|idp-script)/[^/]+\.(js\|py)$` |
| 依赖文件 | `^wx-script/elm_cookie_assets/.*\.(js\|cjs\|json)$` |
| 文件后缀 | `js py cjs json` |
| 分支 | `main` |

表格中的竖线是正则的“或”；实际填写时使用 `|`，不带 Markdown 转义符。白名单仅匹配第一层业务脚本，资源目录通过依赖字段递归同步，保留内部目录结构。`.cjs` 和 `.json` 都必需，尤其不能漏掉 `package.json`。

默认订阅目录下，参数桥应位于：

```text
/ql/data/scripts/lcmovie_YYB-GO-Script-i_main/wx-script/elm_cookie_assets/awsc_bridge.cjs
```

## 配置与运行

- 必需：Python 3.9+；启用 AWSC 时还需 PATH 中的 Node（或通过 `elm_node` 指定路径）。
- `YYB_SERVER`：每行一组 YYB 地址和账号标识，例如 `http://yyb-go:8000@1`。
- `elm_accounts_json`：可选，显式指定账号数组，优先于脚本配置。
- `elm_cookie_ENV_NAME`：可选，默认写入 `ELEME_COOKIE`。
- `elm_ql_url`、`elm_ql_client_id`、`elm_ql_client_secret`：全部填写才写远程青龙；默认写本地青龙。
- `elm_awsc_enabled`：默认启用，设为 `0` 关闭辅助参数，使用基础登录请求。
- `elm_cookie_file` 或 `--output`：可选的额外导出位置，包含 Cookie，不应提交到仓库。

从青龙任务入口执行脚本，以加载青龙环境变量。流程仍包括 YYB 取码、小程序登录、身份 Cookie 校验以及青龙写入读回校验。AWSC 资源补齐不会跳过手机号授权，也不改变其它业务调用。

## AWSC 日志含义与排查

- `登录参数：AWSC 已生成`：参数桥已返回 `bx-ua`、`mini-janus`、`bx-umidtoken` 三个必需请求头。
- `登录参数：AWSC 不可用，本次使用基础登录请求`：辅助参数生成失败，脚本回退到基础登录；不等于 Cookie 一定获取失败。可能原因包括资源位置或名称错误、资源不完整、Node 不可用、参数桥执行失败或超时。
- `需人工`：按日志提示在手机小程序完成后续登录或手机号授权；AWSC 正常不保证所有账号都能登录。

先检查 Node、目录和关键文件：

```bash
node --version
test -f /ql/data/scripts/lcmovie_YYB-GO-Script-i_main/wx-script/elm_cookie_assets/awsc_bridge.cjs
test -f /ql/data/scripts/lcmovie_YYB-GO-Script-i_main/wx-script/elm_cookie_assets/package.json
```

只验证参数桥，不取微信 code、不登录账号、不写 Cookie（会访问 AWSC 的辅助服务并生成本地设备缓存）：

```bash
python3 - <<'PY'
import importlib.util
from pathlib import Path
path = Path('/ql/data/scripts/lcmovie_YYB-GO-Script-i_main/wx-script/elm_cookie.py')
spec = importlib.util.spec_from_file_location('elm_awsc_check', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
headers, umid = module.build_login_context()
assert all(headers.get(key) for key in ('bx-ua', 'mini-janus', 'bx-umidtoken')) and umid, 'AWSC 参数桥验证未通过'
print('AWSC 必需参数检查通过')
PY
```

## 开发验证

在完整仓库根目录执行：

```bash
python3 -m unittest discover -s tests -p 'test_elm*.py'
```

测试使用虚构数据、临时目录和模拟请求，不进行真实账号登录或写入。Node 资源测试禁用网络并使用测试用 UMID，只验证资源可加载和参数生成路径；它不代表服务端接受真实登录请求。缺少 Node 时相关测试明确跳过。
