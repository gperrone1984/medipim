
import requests
from bs4 import BeautifulSoup
import os
import re

class MedipimAPI:
    def __init__(self, username, password):
        self.session = requests.Session()
        self.base_url = "https://platform.medipim.be/en/"  # keep EN locale; site may redirect if different
        self.username = username
        self.password = password
        self.logged_in = False

    def login(self):
        login_url = self.base_url + "login"
        # Basic browser-y headers help some auth flows
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": login_url,
        })
        # 1) GET login page (grab CSRF if present)
        resp_get = self.session.get(login_url, allow_redirects=True, timeout=30)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp_get.content, 'html.parser')
        csrf_token_input = soup.find('input', {'name': '_csrf_token'})
        csrf_token = csrf_token_input['value'] if csrf_token_input else None

        # 2) POST credentials (+ CSRF if present)
        data = {
            '_username': self.username,
            '_password': self.password,
        }
        if csrf_token:
            data['_csrf_token'] = csrf_token

        resp_post = self.session.post(login_url, data=data, allow_redirects=True, timeout=30)

        # Helper: determine if logged in by checking for common authenticated markers
        def looks_logged_in(html, url):
            text = html.lower()
            # Common markers when authenticated: logout links, user/profile menu, absence of login form
            has_logout = "logout" in text or "/logout" in text
            no_login_form = "_username" not in text and "_password" not in text
            left_login_page = "/login" not in url
            return has_logout or (left_login_page and no_login_form)

        if looks_logged_in(resp_post.text, resp_post.url):
            self.logged_in = True
            return True

        # Some sites redirect to home after login; try fetching home to verify
        try:
            home_resp = self.session.get(self.base_url + "home", allow_redirects=True, timeout=30)
            if looks_logged_in(home_resp.text, home_resp.url):
                self.logged_in = True
                return True
        except Exception:
            pass

        # As a final check, if we are not on the login page URL anymore, accept
        if resp_post.ok and "/login" not in resp_post.url:
            self.logged_in = True
            return True

        self.logged_in = False
        return False

        def search_product(self, product_id):
        if not self.logged_in:
            if not self.login():
                return None

        # Try several search patterns Medipim might accept
        search_patterns = [
            f"products?search=refcode[{product_id}]",
            f"products?search={product_id}",
            f"products?search=ean[{product_id}]",
        ]

        for pattern in search_patterns:
            search_url = self.base_url + pattern
            resp = self.session.get(search_url)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.content, 'html.parser')

            # Accept any product link; path might be /en/product?id=... or /en/product/slug
            links = soup.find_all('a', href=lambda href: href and ('/en/product?id=' in href or '/en/product/' in href))
            if links:
                # Prefer links whose text or nearby text contains the product_id
                for link in links:
                    context = (link.get_text() or "") + " " + (" ".join(link.parent.stripped_strings) if link.parent else "")
                    if str(product_id) in context:
                        href = link['href']
                        if not href.startswith('http'):
                            href = self.base_url.rstrip('/') + href
                        return href
                # Fallback: first product link
                href = links[0]['href']
                if not href.startswith('http'):
                    href = self.base_url.rstrip('/') + href
                return href
        return None
