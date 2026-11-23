# Kodi 国内镜像仓库

在 Kodi 中打开「插件」→「从 ZIP 文件安装」，选择对应镜像的 ZIP。正式版使用不带 `.preview` 的目录，测试版使用对应的 `.preview` 目录。若 Kodi 提示来源限制，在 Kodi 设置中允许「未知来源」后再导入。

正式版包含阿里云、北外、中科院软件所、南京大学、南阳理工、清华、中科大、浙江大学和 CERNET MirrorZ。CERNET 是联合镜像入口，会重定向到成员镜像站，并不是独立存储的镜像。浙江大学和 CERNET 的初始正式版包从 Kodi 21 Omega 开始支持。

## 维护仓库

使用 Python 3.10 或更高版本，无需安装 Python 依赖。修改镜像目录中的 `addon.xml` 或 `icon.png` 时，请同时提高插件自身的 `version`。生成器会创建新版本 ZIP、汇总 `addons.xml` 并生成对应的 MD5。历史 ZIP 保留原样。

```sh
python addons_xml_generator.py
python addons_xml_generator.py --check
python -m unittest discover -s tests -v
python sync_kodi_release.py --channel stable --dry-run
python sync_kodi_release.py --channel preview --dry-run
```

Windows 也可用 `py -3` 代替 `python`。不带 `--dry-run` 的同步命令会写入适配变更并生成发布文件。

## GitHub Actions 自动适配

`Sync Kodi mirrors` 在默认分支定时运行，也可在 Actions 页面选择 **Run workflow**，指定 `stable` 或 `preview` 立即检查：

| 频道 | 定时检查（北京时间） | 上游版本 | 发布索引 |
| --- | --- | --- | --- |
| 正式版 `stable` | 每月 1 日 09:17 | 最新正式发布 | `addons.xml` |
| 测试版 `preview` | 每周一 09:37 | 版本号高于当前正式版的最新 Alpha / Beta / RC | `addons.preview.xml` |

脚本从 `xbmc/xbmc` 读取对应发布的 `ADDON_API` 和官方镜像目录，并验证各镜像的 gzip 索引。正式版更新对应的兼容范围，测试版只登记已验证的预发布 API，不会更改正式版索引或 ZIP。没有活跃预发布版时，测试版不生成更新。不可用的镜像会被跳过，检查结果写入运行摘要。

以上频率指本仓库检查 Kodi 上游并生成适配变更的频率，不改变各镜像站的同步周期，也不改变 Kodi 客户端刷新插件列表的频率。

镜像提供匹配的 SHA-256 时，配置会启用该摘要验证。许多镜像只提供 `addons.xml.gz`，这种情况下继续以索引内容作为兼容的变更标记，让 Kodi 能加载仓库。遇到已配置的 SHA-256 验证失败时，同步会跳过该镜像。

有变更时，工作流按频道上传包含新 ZIP 的补丁，并创建或更新 `automation/kodi-stable-sync` 或 `automation/kodi-preview-sync` 分支的 PR。要授权工作流开 PR，在 GitHub 仓库 **Settings → Actions → General → Workflow permissions** 中启用 **Allow GitHub Actions to create and approve pull requests**。手动运行时可以关闭 `open_pr`，只生成补丁；例如 `git apply --index kodi-preview-adaptation.patch`。
