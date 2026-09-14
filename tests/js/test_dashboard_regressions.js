#!/usr/bin/env node
"use strict";

/*
  Testes de regressão do JavaScript REAL do dashboard.

  O arquivo não copia as implementações que deseja testar. Ele lê
  controlefin_dashboard.html, extrai os blocos de produção por marcadores
  estáveis e executa esses blocos em sandboxes Node/vm com fixtures sintéticas.
*/

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { execFileSync } = require("child_process");

const ROOT = path.resolve(__dirname, "..", "..");
const HTML_PATH = path.join(ROOT, "controlefin_dashboard.html");
const FIXTURES = path.join(ROOT, "tests", "fixtures");

const html = fs.readFileSync(HTML_PATH, "utf8");

function sectionBetween(text, startMarker, endMarker, { last = false } = {}) {
  const start = last
    ? text.lastIndexOf(startMarker)
    : text.indexOf(startMarker);

  if (start < 0) {
    throw new Error(`Marcador inicial não encontrado: ${startMarker}`);
  }

  const end = text.indexOf(endMarker, start);
  if (end < 0) {
    throw new Error(`Marcador final não encontrado: ${endMarker}`);
  }

  return text.slice(start, end);
}

function context(base = {}) {
  const ctx = vm.createContext({
    console,
    Map,
    Set,
    WeakMap,
    Date,
    Math,
    Number,
    String,
    Object,
    Array,
    JSON,
    RegExp,
    Boolean,
    ...base,
  });
  return ctx;
}

function runBlock(code, ctx, filename = "dashboard-extracted.js") {
  vm.runInContext(code, ctx, { filename });
}

function testSyntax() {
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  assert.ok(scripts.length > 0, "dashboard precisa conter script inline");

  const tmp = path.join(__dirname, "_dashboard_syntax_check.js");
  fs.writeFileSync(tmp, scripts[scripts.length - 1][1], "utf8");

  try {
    execFileSync(process.execPath, ["--check", tmp], {
      stdio: "pipe",
    });
  } finally {
    fs.rmSync(tmp, { force: true });
  }
}

function testCardBankslip() {
  const block = sectionBetween(
    html,
    "  const cardBankslipDetectionCache = new WeakMap();",
    "\n  function searchableTransactionText"
  );

  const ctx = context();
  runBlock(block, ctx, "cardbankslip-production.js");

  const positive = [
    { type: "CardBankslip" },
    { operationType: "CardBankSlip" },
    { paymentMethod: "Card_Bank_Slip" },
    { description: "Pagamento Card Bank Slip consolidado" },
    { metadata: "foo CardBankslip bar" },
  ];

  positive.forEach((row) => {
    assert.strictEqual(
      ctx.isCardBankslipTransaction(row),
      true,
      `deveria detectar ${JSON.stringify(row)}`
    );
  });

  const negative = [
    { paymentMethod: "BANK_SLIP", description: "Boleto comum" },
    { accountType: "CREDIT", description: "SPOTIFY" },
  ];

  negative.forEach((row) => {
    assert.strictEqual(
      ctx.isCardBankslipTransaction(row),
      false,
      `não deveria detectar ${JSON.stringify(row)}`
    );
  });
}

function testInstallmentsOnePerMonth() {
  const block = sectionBetween(
    html,
    "  function localCurrentMonth() {",
    "\n  function setTableFromFilename"
  );

  const rawTransactions = JSON.parse(
    fs.readFileSync(
      path.join(FIXTURES, "installments_same_month.json"),
      "utf8"
    )
  );

  const state = {
    currency: "BRL",
    manualTransactions: [],
    installmentAnalysisTransactions: [],
    installmentAnalysisStats: {
      groups: 0,
      actual: 0,
      projected: 0,
    },
    pendingCardDuplicateKeys: new Set(),
  };

  const ctx = context({
    state,
    table(name) {
      return name === "transactions" ? rawTransactions : [];
    },
    num(v) {
      if (v === null || v === undefined || v === "") return null;
      const n = Number(v);
      return Number.isFinite(n) ? n : null;
    },
    val(v, fallback = 0) {
      const n = this.num ? this.num(v) : Number(v);
      return Number.isFinite(n) ? n : fallback;
    },
    transactionCurrency(r) {
      return String(r.currencyCode || "UNKNOWN");
    },
    transactionMonth(r) {
      return r.month || String(r.date || "").slice(0, 7);
    },
    transactionAbsAmount(r) {
      return Math.abs(Number(r.amount || 0));
    },
    originalMerchant(r) {
      return (
        r.merchant__name ||
        r.merchant__businessName ||
        r.merchantName ||
        r.description ||
        "Não identificado"
      );
    },
    txKey(r) {
      return r.id ? `id:${r.id}` : "fallback";
    },
  });

  // Rebind val sem depender de `this` no contexto VM.
  ctx.val = function val(v, fallback = 0) {
    if (v === null || v === undefined || v === "") return fallback;
    const n = Number(v);
    return Number.isFinite(n) ? n : fallback;
  };

  runBlock(block, ctx, "installments-production.js");

  ctx.buildInstallmentAnalysisTransactions();

  const installments = state.installmentAnalysisTransactions
    .filter((r) => r.installmentAnalysis)
    .sort((a, b) => a.month.localeCompare(b.month));

  assert.strictEqual(installments.length, 3);
  assert.deepStrictEqual(
    Array.from(installments, (r) => r.month),
    ["2026-08", "2026-09", "2026-10"]
  );

  const monthCounts = new Map();
  installments.forEach((row) => {
    monthCounts.set(row.month, (monthCounts.get(row.month) || 0) + 1);
  });

  assert.ok(
    [...monthCounts.values()].every((count) => count === 1),
    "uma compra parcelada deve contribuir no máximo uma vez por mês"
  );

  assert.strictEqual(
    new Set(installments.map((r) => r.installmentGroupId)).size,
    1,
    "1/3, 2/3 e 3/3 precisam pertencer ao mesmo grupo"
  );
}

function testEffectiveCashflow() {
  const block = sectionBetween(
    html,
    "  function aggregateCashflow() {",
    "\n  function renderCardActivity()",
    { last: true }
  );

  const rows = [
    {
      _month: "2026-08",
      _kind: "EXPENSE",
      _expense: 100,
      _isCardBankslip: false,
      _category: "Food",
    },
    {
      _month: "2026-08",
      _kind: "EXPENSE",
      _expense: 0,
      _isCardBankslip: false,
      _category: "Food",
    },
    {
      _month: "2026-08",
      _kind: "CARD_BILL",
      _expense: 100,
      _isCardBankslip: true,
      _category: "Other",
    },
    {
      _month: "2026-08",
      _kind: "EXPENSE",
      _expense: 200,
      _isCardBankslip: false,
      _category: "Ignored",
    },
    {
      _month: "2026-08",
      _kind: "INCOME",
      _income: 1000,
      _isIncomeReimbursement: false,
    },
    {
      _month: "2026-08",
      _kind: "INCOME",
      _income: 40,
      _isIncomeReimbursement: true,
    },
  ];

  const ctx = context({
    windowTransactions() {
      return rows;
    },
    isDefaultIgnoredSpendingCategory(r) {
      return r._category === "Ignored";
    },
  });

  runBlock(block, ctx, "cashflow-production.js");

  const result = ctx.aggregateCashflow();
  assert.strictEqual(result.length, 1);
  assert.strictEqual(result[0].month, "2026-08");
  assert.strictEqual(result[0].inflow, 1000);
  assert.strictEqual(result[0].outflow, 100);
  assert.strictEqual(result[0].net, 900);
}

function testFutureProjectionSalaryFixedInstallmentsAndNet() {
  const block = sectionBetween(
    html,
    "  function futureProjectionMonths(",
    "\n  function renderMeta()"
  );

  const state = {
    currency: "BRL",
    futureProjectionHorizon: 3,
    installmentAnalysisTransactions: [
      {
        id: "i2",
        installmentAnalysis: true,
        installmentProjected: true,
        installmentGroupId: "g1",
        sourceTransactionId: "src1",
        creditCardMetadata__installmentNumber: 2,
        creditCardMetadata__totalInstallments: 3,
        month: "2026-10",
        date: "2026-10-10",
        currencyCode: "BRL",
        accountType: "CREDIT",
        normalizedExpense: 100,
        description: "Compra parcelada",
        merchantName: "Loja",
        status: "PROJECTED",
      },
      {
        id: "i3",
        installmentAnalysis: true,
        installmentProjected: true,
        installmentGroupId: "g1",
        sourceTransactionId: "src1",
        creditCardMetadata__installmentNumber: 3,
        creditCardMetadata__totalInstallments: 3,
        month: "2026-11",
        date: "2026-11-10",
        currencyCode: "BRL",
        accountType: "CREDIT",
        normalizedExpense: 100,
        description: "Compra parcelada",
        merchantName: "Loja",
        status: "PROJECTED",
      },
    ],
  };

  const historical = [
    {
      id: "salary-old",
      date: "2026-08-30",
      month: "2026-08",
      currencyCode: "BRL",
      accountId: "bank1",
      payer_name: "Empresa A",
      mock: "salary-old",
    },
    {
      id: "salary-new",
      date: "2026-09-30",
      month: "2026-09",
      currencyCode: "BRL",
      accountId: "bank1",
      payer_name: "Empresa A",
      mock: "salary-new",
    },
    {
      id: "fixed-old",
      date: "2026-08-05",
      month: "2026-08",
      currencyCode: "BRL",
      accountId: "bank1",
      mock: "fixed-old",
    },
    {
      id: "fixed-new",
      date: "2026-09-05",
      month: "2026-09",
      currencyCode: "BRL",
      accountId: "bank1",
      mock: "fixed-new",
    },
  ];

  function effectiveTransaction(r) {
    if (r.mock === "salary-old" || r.mock === "salary-new") {
      const amount = r.mock === "salary-new" ? 5000 : 4800;
      return {
        ...r,
        _kind: "INCOME",
        _income: amount,
        _isIncomeReimbursement: false,
        _category: "Salário",
        _categoryStorage: "@cf:salary",
        _merchant: "Empresa A",
        _institution: "Banco",
        _accountLabel: "Banco · Conta",
        _month: r.month,
        _incomeSourceCategoryRule: {
          id: "salary-rule-a",
          sourceLabel: "Empresa A",
        },
      };
    }

    if (r.mock === "fixed-old" || r.mock === "fixed-new") {
      const amount = r.mock === "fixed-new" ? 130 : 120;
      return {
        ...r,
        _kind: "EXPENSE",
        _expenseClass: "FIXED",
        _expense: amount,
        _grossExpense: amount,
        _isCardBankslip: false,
        _merchant: "ISP",
        _category: "Internet",
        _categoryStorage: "@cf:internet",
        _method: "Boleto",
        _institution: "Banco",
        _accountLabel: "Banco · Conta",
        _month: r.month,
        _fixedExpenseRule: {
          id: "fixed-internet",
          sourceLabel: "Internet",
        },
      };
    }

    if (r.installmentAnalysis) {
      return {
        ...r,
        _kind: "EXPENSE",
        _expense: 100,
        _grossExpense: 100,
        _isCardBankslip: false,
        _merchant: "Loja",
        _category: "Compras",
        _institution: "Banco",
        _accountLabel: "Banco · Cartão",
        _month: r.month,
      };
    }

    throw new Error(`fixture inesperada: ${JSON.stringify(r)}`);
  }

  const ctx = context({
    state,
    localCurrentMonth() {
      return "2026-09";
    },
    shiftYearMonth(value, delta) {
      const [y, m] = value.split("-").map(Number);
      const d = new Date(Date.UTC(y, m - 1 + delta, 1));
      return `${d.getUTCFullYear()}-${String(
        d.getUTCMonth() + 1
      ).padStart(2, "0")}`;
    },
    transactionCurrency(r) {
      return r.currencyCode || "BRL";
    },
    transactionMonth(r) {
      return r.month || String(r.date || "").slice(0, 7);
    },
    sourceTransactions() {
      return historical;
    },
    isUsableTransaction() {
      return true;
    },
    isIgnoredYieldIncome() {
      return false;
    },
    installmentMetadata() {
      return null;
    },
    isDefaultIgnoredSpendingCategory() {
      return false;
    },
    isTerminalInvalidStatus() {
      return false;
    },
    isCardBankslipTransaction() {
      return false;
    },
    fixedSignature(v) {
      return String(v || "").toLowerCase();
    },
    positiveInteger(v) {
      const n = Number(v);
      return Number.isFinite(n) && n >= 1 ? Math.trunc(n) : null;
    },
    effectiveTransaction,
    transactionPayerName(r) {
      return r.payer_name || "";
    },
    incomeSourceIdentity(r) {
      const payer = r.payer_name || "";
      return payer
        ? {
            sourceType: "payer",
            sourceSignature: payer.toLowerCase(),
            sourceLabel: payer,
          }
        : null;
    },
    isSalaryCategory(value) {
      return value === "@cf:salary" || value === "Salário";
    },
    uiLocale() {
      return "pt-BR";
    },
    val(v, fallback = 0) {
      const n = Number(v);
      return Number.isFinite(n) ? n : fallback;
    },
    money(v) {
      return String(v);
    },
    integer(v) {
      return String(v);
    },
    esc(v) {
      return String(v ?? "");
    },
    shortDate(v) {
      return v;
    },
    compact(v) {
      return String(v);
    },
    $(id) {
      return null;
    },
    hiddenSeries() {
      return new Set();
    },
    legendMarkup() {
      return "";
    },
    bindLegend() {},
  });

  runBlock(block, ctx, "projections-production.js");

  const p = ctx.buildFutureProjection();

  assert.strictEqual(p.salaryReferences.length, 1);
  assert.strictEqual(p.salaryReferences[0].amount, 5000);

  assert.strictEqual(p.fixedReferences.length, 1);
  assert.strictEqual(p.fixedReferences[0].amount, 130);

  assert.strictEqual(p.rows.length, 3);
  assert.strictEqual(p.rows[0].month, "2026-10");
  assert.strictEqual(p.rows[0].salary, 5000);
  assert.strictEqual(p.rows[0].expenses, 230);
  assert.strictEqual(p.rows[0].net, 4770);

  assert.strictEqual(p.rows[1].salary, 5000);
  assert.strictEqual(p.rows[1].expenses, 230);
  assert.strictEqual(p.rows[1].net, 4770);

  assert.strictEqual(p.rows[2].salary, 5000);
  assert.strictEqual(p.rows[2].expenses, 130);
  assert.strictEqual(p.rows[2].net, 4870);

  assert.strictEqual(p.salaryTotal, 15000);
  assert.strictEqual(p.expenseTotal, 590);
  assert.strictEqual(p.netTotal, 14410);
}

const tests = [
  ["Sintaxe do JavaScript", testSyntax],
  ["CardBankslip", testCardBankslip],
  ["Parcelamento: uma parcela por mês", testInstallmentsOnePerMonth],
  ["Fluxo de caixa efetivo", testEffectiveCashflow],
  ["Projeções: salário, fixos, parcelas e líquido", testFutureProjectionSalaryFixedInstallmentsAndNet],
];

let failed = 0;

for (const [name, fn] of tests) {
  try {
    fn();
    console.log(`OK  ${name}`);
  } catch (error) {
    failed += 1;
    console.error(`FAIL ${name}`);
    console.error(error.stack || error);
  }
}

if (failed) {
  console.error(`\n${failed} teste(s) JavaScript falharam.`);
  process.exit(1);
}

console.log(`\n${tests.length} grupos JavaScript passaram.`);
