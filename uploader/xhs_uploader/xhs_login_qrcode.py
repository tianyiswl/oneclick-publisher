import datetime
import json
import qrcode
from time import sleep

from xhs import XhsClient

from uploader.xhs_uploader.main import sign


def mask_cookie(cookie: str) -> str:
    if not cookie:
        return "<空>"
    if len(cookie) <= 12:
        return "<已隐藏>"
    return f"{cookie[:6]}...{cookie[-6:]}"


# pip install qrcode
if __name__ == '__main__':
    xhs_client = XhsClient(sign=sign, timeout=60)
    print(datetime.datetime.now())
    qr_res = xhs_client.get_qrcode()
    qr_id = qr_res["qr_id"]
    qr_code = qr_res["code"]

    qr = qrcode.QRCode(version=1, error_correction=qrcode.ERROR_CORRECT_L,
                       box_size=50,
                       border=1)
    qr.add_data(qr_res["url"])
    qr.make()
    qr.print_ascii()

    while True:
        check_qrcode = xhs_client.check_qrcode(qr_id, qr_code)
        print(check_qrcode)
        sleep(1)
        if check_qrcode["code_status"] == 2:
            print("登录成功，登录信息已隐藏，避免在日志中暴露账号 Cookie。")
            print("Cookie 摘要：" + mask_cookie(xhs_client.cookie))
            break

    print(json.dumps(xhs_client.get_self_info(), indent=4))
