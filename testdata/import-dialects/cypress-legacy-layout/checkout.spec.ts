/// <reference types="cypress" />
// A REAL Cypress suite carrying the filename that used to prove "this is Playwright".
//
// Cypress <= 9's default layout is cypress/integration/**/*.spec.ts, so this exact shape arrives at
// both import channels. Before engine detection, the Playwright parser read it, found no `test(`
// (Cypress writes `it(`), produced zero steps, and the run reported
//   imported 0 test(s), 0 step(s) ... exit 0
// with a report claiming "engine":"playwright" — a green success over a suite that had vanished.
//
// This fixture exists to make that outcome impossible to reintroduce. It is NOT yet transpiled: the
// Cypress dialect is the next PR. What is pinned here is that the file is RECOGNISED as Cypress,
// NAMED in the report, and that the run refuses to exit 0 over it.

describe('checkout', () => {
  beforeEach(() => {
    cy.intercept('POST', '/api/analytics', { statusCode: 204 }).as('analytics');
  });

  it('pays with a saved card', () => {
    cy.visit('/billing');
    cy.get('[data-cy=invoice-4471]').click();
    cy.get('#card-number').type('4242424242424242');
    cy.contains('Pay now').click();
    cy.get('.receipt').should('be.visible');
    cy.get('.receipt-total').should('have.text', '$42.00');
  });

  it('rejects an expired card', () => {
    cy.visit('/billing');
    cy.get('[data-cy=invoice-4471]').click();
    cy.get('#card-number').type('4000000000000069');
    cy.contains('Pay now').click();
    cy.get('.error').should('contain', 'expired');
    cy.get('.receipt').should('not.exist');
  });
});
