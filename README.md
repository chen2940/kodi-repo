# Kodi 国内镜像仓库

在 Kodi 中打开「插件」→「从 ZIP 文件安装」，选择 `repository.kodi.mirror.*` 目录下对应镜像的 ZIP。若 Kodi 提示来源限制，在 Kodi 设置中允许「未知来源」后再导入。

## 维护仓库

使用 Python 3.10 或更高版本，无需安装 Python 依赖。修改镜像目录中的 `addon.xml` 或 `icon.png` 时，请同时提高插件自身的 `version`。生成器会创建新版本 ZIP、汇总 `addons.xml` 并生成对应的 MD5。历史 ZIP 保留原样。

```sh
python addons_xml_generator.py
python addons_xml_generator.py --check
python -m unittest discover -s tests -v
python sync_kodi_release.py --dry-run
```

Windows 也可用 `py -3` 代替 `python`。不带 `--dry-run` 的同步命令会写入适配变更并生成发布文件。

## GitHub Actions 自动适配

`Sync latest stable Kodi` 在默认分支每 6 小时运行一次，也可以在 Actions 页面用 **Run workflow** 立即运行。脚本从 `xbmc/xbmc` 的最新稳定发布读取 `ADDON_API` 和官方镜像目录，验证各镜像的 gzip 索引。现有系列扩展到已验证的 API 版本，新系列以该 API 版本为起点。镜像尚未同步时会被跳过，检查结果会写入运行摘要。

镜像提供匹配的 SHA-256 时，配置会启用该摘要验证。许多镜像只提供 `addons.xml.gz`，这种情况下继续以索引内容作为兼容的变更标记，让 Kodi 能加载仓库。遇到已配置的 SHA-256 验证失败时，同步会跳过该镜像。

有变更时，工作流会上传包含新 ZIP 的 `kodi-adaptation.patch`，并创建或更新 `automation/kodi-stable-sync` 分支的 PR。要授权工作流开 PR，在 GitHub 仓库 **Settings → Actions → General → Workflow permissions** 中启用 **Allow GitHub Actions to create and approve pull requests**。手动运行时可以关闭 `open_pr`，只生成补丁。在仓库根目录用 `git apply --index kodi-adaptation.patch` 可应用下载的补丁。
