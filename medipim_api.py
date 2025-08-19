
import requests
from bs4 import BeautifulSoup
import os
import re

class MedipimAPI:
    def __init__(self, username, password):
        self.session = requests.Session()
        self.base_url = "https://platform.medipim.be/en/"
        self.username = username
        self.password = password
        self.logged_in = False

    def login(self):
        login_url = self.base_url + "login"
        response = self.session.get(login_url)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Find CSRF token if present
        csrf_token_input = soup.find('input', {'name': '_csrf_token'})
        csrf_token = csrf_token_input['value'] if csrf_token_input else None

        login_data = {
            '_username': self.username,
            '_password': self.password,
        }
        
        if csrf_token:
            login_data['_csrf_token'] = csrf_token
            
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
        }
        response = self.session.post(login_url, data=login_data, headers=headers)
        
        # Check if login was successful by looking for user name or dashboard elements
        if "Donique May" in response.text or "/en/home" in response.url:
            self.logged_in = True
            return True
        else:
            self.logged_in = False
            return False

    def search_product(self, product_id):
        if not self.logged_in:
            if not self.login():
                return None
        
        search_url = self.base_url + f"products?search=refcode[{product_id}]"
        response = self.session.get(search_url)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Find the link to the product details page
        # Look for links that contain the product ID
        product_links = soup.find_all('a', href=lambda href: href and '/en/product?id=' in href)
        
        for link in product_links:
            # Check if the link text or nearby text contains our product ID
            link_text = link.get_text()
            if product_id in link_text:
                return self.base_url.rstrip('/') + link['href']
        
        return None

    def get_image_url(self, product_detail_url, size="1500x1500"):
        if not self.logged_in:
            if not self.login():
                return None

        response = self.session.get(product_detail_url)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Navigate to media section
        media_link = soup.find('a', href=lambda href: href and 'media' in href.lower())
        if not media_link:
            # Try to find media tab or section
            media_elements = soup.find_all(text=re.compile(r'Media', re.IGNORECASE))
            for element in media_elements:
                parent = element.parent
                if parent.name == 'a' and parent.get('href'):
                    media_link = parent
                    break
        
        if media_link:
            media_href = media_link['href']
            if not media_href.startswith('http'):
                media_url = self.base_url.rstrip('/') + '/' + media_href.lstrip('/')
            else:
                media_url = media_href
                
            response = self.session.get(media_url)
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Look for image URLs - try different patterns
            # Pattern 1: Direct links to huge/large images
            image_links = soup.find_all('a', href=lambda href: href and '/media/huge/' in href)
            if not image_links:
                image_links = soup.find_all('a', href=lambda href: href and '/media/large/' in href)
            
            if image_links:
                return image_links[0]['href']
            
            # Pattern 2: Look for image URLs in the page content
            page_text = response.text
            huge_pattern = r'https://assets\.medipim\.be/media/huge/[a-f0-9]+\.jpeg'
            large_pattern = r'https://assets\.medipim\.be/media/large/[a-f0-9]+\.jpeg'
            
            huge_matches = re.findall(huge_pattern, page_text)
            if huge_matches:
                return huge_matches[0]
                
            large_matches = re.findall(large_pattern, page_text)
            if large_matches:
                return large_matches[0]
        
        return None

    def download_image(self, image_url, save_path):
        if not self.logged_in:
            if not self.login():
                return False

        try:
            response = self.session.get(image_url, stream=True)
            if response.status_code == 200:
                with open(save_path, 'wb') as f:
                    for chunk in response.iter_content(1024):
                        f.write(chunk)
                return True
        except Exception as e:
            print(f"Error downloading image: {e}")
            
        return False


