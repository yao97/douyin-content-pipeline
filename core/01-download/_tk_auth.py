# -*- coding: utf-8 -*-
"""
给工具的 tester（Params）补上正式鉴权参数。

背景（踩过的坑）：
  src/testers/Params 读的是 Volume/test_cookie.ini —— 那份 cookie 可能已过期，
  而且它**不会注入 uifid 和 msToken**。缺这两个值时抖音只放第一页，
  第二页开始按"未登录"风控：响应头 whale-decision-custom: black_no_login，
  body 只有 17 字节的 {"status_code":0}，工具日志报"配置文件 cookie 参数未登录，数据获取已提前结束"。

修法（与工具 Parameter.update_params_offline / set_uif_id 一致）：
  1. 用 Volume/settings.json 里的正式 cookie 覆盖 header 与 cookie_str
  2. uifid 从 cookie 的 UIFID 键取值，写进 API.params 和请求头
  3. msToken 在无法从 cookie 取到时，调工具的 MsToken.get_real_ms_token 在线获取
"""
import json
import pathlib

ROOT = pathlib.Path(r"D:\视频\自媒体视频库")
SETTINGS = ROOT / "_tools" / "TikTokDownloader" / "Volume" / "settings.json"


def read_cookie(settings_path: pathlib.Path = SETTINGS):
    """读正式 cookie，返回 (cookie_str, cookie_dict, uifid)"""
    data = json.loads(pathlib.Path(settings_path).read_text(encoding="utf-8-sig"))
    cookie_str = data.get("cookie", "") or ""
    cookie_dict = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookie_dict[k.strip()] = v.strip()
    uifid = next((v for k, v in cookie_dict.items() if k.lower() == "uifid"), "")
    return cookie_str, cookie_dict, uifid


async def inject(params, settings_path: pathlib.Path = SETTINGS, verbose=True) -> dict:
    """把 cookie / uifid / msToken 注入 params 与 API.params，返回注入信息"""
    from src.encrypt.msToken import MsToken
    from src.interface.template import API

    cookie_str, cookie_dict, uifid = read_cookie(settings_path)

    if cookie_str:
        params.cookie_str = cookie_str
        params.headers["Cookie"] = cookie_str
    if uifid:
        params.uifid = uifid
        params.headers["uifid"] = uifid
        API.params["uifid"] = uifid

    ms = ""
    try:
        token = await MsToken.get_real_ms_token(params.logger, params.headers, proxy=None)
        ms = (token or {}).get("msToken") or ""
    except Exception as e:  # 网络异常不应致命
        if verbose:
            print(f"[警告] 在线获取 msToken 异常 {type(e).__name__}: {str(e)[:80]}")
    if not ms:
        ms = MsToken.get_fake_ms_token().get("msToken", "")
        if verbose:
            print("[警告] 在线 msToken 获取失败，退化用随机值（可能仍被风控）")
    API.params["msToken"] = ms
    params.ms_token = ms

    info = {"cookie_len": len(cookie_str), "uifid_len": len(uifid), "ms_token_len": len(ms)}
    if verbose:
        print(f"注入参数：uifid={info['uifid_len']}字符 "
              f"msToken={info['ms_token_len']}字符 cookie={info['cookie_len']}字符")
    return info
