// A representative Selenium/.NET suite used as an IMPORT fixture (PROD-IMPORT, PR-4).
//
// Same WebDriver API, different spelling: FindElement / SendKeys / Navigate().GoToUrl, By.Id rather
// than By.ID, and the Page Object annotation is [FindsBy(How = How.Id, Using = "...")]. If the model
// really is one model, the same assertions that hold for Java hold here with only the token table
// swapped — which is what the gate checks.
using System;
using NUnit.Framework;
using OpenQA.Selenium;
using OpenQA.Selenium.Support.PageObjects;
using OpenQA.Selenium.Support.UI;

namespace Example.E2E
{
    public class CheckoutTests
    {
        private IWebDriver driver;

        [FindsBy(How = How.Id, Using = "pay-now")]
        private IWebElement payButton;

        [Test]
        public void PaysWithASavedCard()
        {
            driver.Navigate().GoToUrl("https://shop.example.com/billing");
            driver.FindElement(By.CssSelector("[data-invoice='4471']")).Click();
            driver.FindElement(By.Id("card-number")).SendKeys(Environment.GetEnvironmentVariable("TEST_CARD"));
            payButton.Click();
        }

        [Test]
        public void SignsIn()
        {
            driver.Navigate().GoToUrl("https://shop.example.com/login");
            driver.FindElement(By.Name("username")).SendKeys("qa_admin");
            new WebDriverWait(driver, TimeSpan.FromSeconds(10))
                .Until(ExpectedConditions.ElementIsVisible(By.LinkText("Sign in")));
            driver.FindElement(By.LinkText("Sign in")).Click();
        }
    }
}
