// A representative Selenium/Java suite used as an IMPORT fixture (PROD-IMPORT, PR-4).
//
// Java is where Selenium's Page Object pattern actually lives, and it is the reason Java and C# are a
// separate PR from Python and JS: the locator sits on an ANNOTATION and the element in a FIELD, so
// the line that ACTS carries only a field name. This fixture holds both cases on purpose —
//   - `payButton` is declared HERE, so it can be joined and must bind;
//   - `confirmDialog` is declared in another file, so it cannot, and must be reported BY NAME.
package com.example.e2e;

import org.openqa.selenium.By;
import org.openqa.selenium.WebDriver;
import org.openqa.selenium.WebElement;
import org.openqa.selenium.support.FindBy;
import org.openqa.selenium.support.ui.ExpectedConditions;
import org.openqa.selenium.support.ui.WebDriverWait;
import org.junit.jupiter.api.Test;

public class CheckoutTest {
    private WebDriver driver;

    @FindBy(id = "pay-now")
    private WebElement payButton;

    // declared in BillingPage.java, not here — deliberately unresolvable
    private WebElement confirmDialog;

    @Test
    public void paysWithASavedCard() {
        driver.get("https://shop.example.com/billing");
        driver.findElement(By.cssSelector("[data-invoice='4471']")).click();
        driver.findElement(By.id("card-number")).sendKeys(System.getenv("TEST_CARD"));
        payButton.click();
        confirmDialog.click();
    }

    @Test
    public void signsIn() {
        driver.get("https://shop.example.com/login");
        driver.findElement(By.name("username")).sendKeys("qa_admin");
        new WebDriverWait(driver, java.time.Duration.ofSeconds(10))
            .until(ExpectedConditions.elementToBeClickable(By.linkText("Sign in")));
        driver.findElement(By.linkText("Sign in")).click();
    }

    // NOT a test: no @Test annotation. Matching a bare `void x()` would turn this into one.
    public void helperThatIsNotATest() {
        driver.findElement(By.id("nope")).click();
    }
}
