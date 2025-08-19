import requests
from bs4 import BeautifulSoup
import os
import re

class MedipimAPI:
    def __init__(self, username, password):
        self.session = requests.Session()
        self.base_url = "https://platform.medipim.be/en/"  # URL base
        self.username = username
        self.password = password
        self.logged_in = False

    def login(self):
        login_url = self.base_url + "login"
        # Header tipo browser
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": login_url,
        })
        # GET login page per token
        resp_get = self.session.get(login_url, allow_redirects=True, timeout=30)
        soup = BeautifulSoup(resp_get.content, 'html.parser')
        csrf_token_input = soup.find('input', {'name': '_csrf_token'})
        csrf_token = csrf_token_input['value'] if csrf_token_input else None

        # POST credenziali
        data = {
            '_username': self.username,
            '_password': self.password,
        }
        if csrf_token:
            data['_csrf_token'] = csrf_token

        resp_post = self.session.post(login_url, data=data, allow_redirects=True, timeout=30)

        def looks_logged_in(html, url):
            text = html.lower()
            has_logout = "logout" in text or "/logout" in text
            no_login_form = "_username" not in text and "_password" not in text
            left_login_page = "/login" not in url
            return has_logout or (left_login_page and no_login_form)

        if looks_logged_in(resp_post.text, resp_post.url):
            self.logged_in = True
            return True

        try:
            home_resp = self.session.get(self.base_url + "home", allow_redirects=True, timeout=30)
            if looks_logged_in(home_resp.text, home_resp.url):
                self.logged_in = True
                return True
        except Exception:
            pass

        if resp_post.ok and "/login" not in resp_post.url:
            self.logged_in = True
            return True

        self.logged_in = False
        return False

    def search_product(self, product_id):
        if not self.logged_in and not self.login():
            return None

        # Usa solo numero/lettere pure (toglie bullet, spazi, simboli)
        clean_id = re.sub(r'[^0-9A-Za-z]', '', str(product_id))

        search_patterns = [
            f"products?search=refcode[{clean_id}]",
            f"products?search={clean_id}",
            f"products?search=ean[{clean_id}]",
        ]

        for pattern in search_patterns:
            search_url = self.base_url + pattern
            resp = self.session.get(search_url)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.content, 'html.parser')
            links = soup.find_all('a', href=lambda href: href and ('/en/product?id=' in href or '/en/product/' in href))
            if links:
                for link in links:
                    context = (link.get_text() or "") + " " + (" ".join(link.parent.stripped_strings) if link.parent else "")
                    # Confronto con numero puro
                    if clean_id in re.sub(r'[^0-9A-Za-z]', '', context):
                        href = link['href']
                        if not href.startswith('http'):
                            href = self.base_url.rstrip('/') + href
                        return href
                # fallback: primo link
                href = links[0]['href']
                if not href.startswith('http'):
                    href = self.base_url.rstrip('/') + href
                return href

        return None

    def get_image_url(self, product_detail_url, size="1500x1500"):
        if not self.logged_in and not self.login():
            return None

        resp_detail = self.session.get(product_detail_url)
        soup = BeautifulSoup(resp_detail.content, 'html.parser')

        html = resp_detail.text
        url_patterns = [
            r"https://assets\.medipim\.be/media/huge/[a-f0-9]+\.(?:jpeg|jpg|png|webp)(?:\?[^\" \t<>]*)?",
            r"https://assets\.medipim\.be/media/large/[a-f0-9]+\.(?:jpeg|jpg|png|webp)(?:\?[^\" \t<>]*)?",
        ]
        for pat in url_patterns:
            m = re.search(pat, html)
            if m:
                return m.group(0)

        media_link = soup.find('a', href=lambda href: href and 'media' in href.lower())
        if not media_link:
            media_elements = soup.find_all(string=re.compile(r'Media', re.IGNORECASE))
            for element in media_elements:
                parent = element.parent
                if parent and parent.name == 'a' and parent.get('href'):
                    media_link = parent
                    break

        if not media_link:
            candidate = product_detail_url.rstrip('/') + '/media'
            resp_media = self.session.get(candidate)
            if resp_media.ok:
                text = resp_media.text
                for pat in url_patterns:
                    m = re.search(pat, text)
                    if m:
                        return m.group(0)
        else:
            href = media_link.get('href')
            if not href:
                return None
            media_url = href if href.startswith('http') else self.base_url.rstrip('/') + '/' + href.lstrip('/')
            resp_media = self.session.get(media_url)
            if resp_media.ok:
                text = resp_media.text
                for pat in url_patterns:
                    m = re.search(pat, text)
                    if m:
                        return m.group(0)

        return None

    def download_image(self, image_url, save_path):
        if not self.logged_in and not self.login():
            return (False, None, None)
        try:
            headers = {
                "User-Agent": "Mozilla/5.0",
                "Referer": self.base_url,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            }
            response = self.session.get(image_url, stream=True, headers=headers, allow_redirects=True, timeout=60)
            ctype = response.headers.get("Content-Type", "")
            if response.status_code == 200 and ctype.startswith("image/"):
                with open(save_path, 'wb') as f:
                    for chunk in response.iter_content(1024):
                        f.write(chunk)
                return (True, response.status_code, ctype)
            else:
                return (False, response.status_code, ctype)
        except Exception as e:
            print(f"Error downloading image: {e}")
            return (False, None, None)
