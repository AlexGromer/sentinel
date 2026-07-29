# A representative selenium-python suite used as an IMPORT fixture (PROD-IMPORT, PR-3).
#
# Deliberately ordinary: this is what a team migrating off manual QA actually has. It carries the
# whole Selenium mismatch surface — every locator is structural (Selenium has no semantic locator at
# all), a secret arrives from the environment, and an EXPLICIT WebDriverWait sits in the middle,
# which is the class that disappears under implicit waiting and must be reported rather than absorbed.
import os

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait


def test_login_and_reach_dashboard():
    driver = webdriver.Chrome()
    driver.get("https://shop.example.com/login")
    driver.find_element(By.ID, "username").send_keys("qa_admin")
    driver.find_element(By.NAME, "password").send_keys(os.environ["LOGIN_PASSWORD"])
    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#sign-in")))
    driver.find_element(By.CSS_SELECTOR, "#sign-in").click()


def test_pay_an_invoice():
    driver = webdriver.Chrome()
    driver.get("https://shop.example.com/billing")
    driver.find_element(By.XPATH, "//tr[@data-invoice='4471']//button").click()
    Select(driver.find_element(By.ID, "card")).select_by_visible_text("Visa ****4242")
    driver.find_element(By.LINK_TEXT, "Pay now").click()
