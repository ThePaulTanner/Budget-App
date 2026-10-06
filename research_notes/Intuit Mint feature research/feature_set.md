# Intuit Mint Feature Set (roughly 2015 to 2024): Inventory for a Rebuild

> **Research method note.** The network egress proxy for this session blocked direct page fetches for almost every relevant domain: mint.intuit.com, help.mint.com, web.archive.org, en.wikipedia.org, nerdwallet.com, annielytics.com, marblejar.net, thefinancialfreedomproject.com and couragepassion.wordpress.com. Only github.com/gist.github.com could be fetched in full. Most cited findings below therefore come from **search-engine result summaries** of the linked pages, not from full reads of those pages. Treat them as medium confidence and re-check them against the archived help pages before building anything that depends on an exact detail. Points that come from my own background knowledge and that I could not confirm with a source are kept in the **Inferences** or **Gaps** sections and labelled "unverified".

---

## 1. Account aggregation (supported account types, refresh, manual accounts)

### Takeaway
Mint pulled in data from bank, credit card, loan, investment and retirement accounts. It also tracked property: home value from Zillow's Zestimate (partnership since 2010) and manual assets such as vehicles, cash and valuables. Linked accounts refreshed automatically about once a day. Mint used Yodlee as its data provider before Intuit bought it in 2009, then moved to Intuit's own connections.

### Cited Findings
- Mint partnered with Zillow in September 2010 to show a home's "Zestimate" as an asset value — [Inman](https://www.inman.com/2010/09/23/mintcom-partners-with-zillow/); [Zillow news](https://www.zillow.com/news/whats-my-home-worth-mint-com-taps-zillow-com-for-real-estate-valuations/); [CreditInfoCenter](https://www.creditinfocenter.com/breaking-mint-partners-with-zillow-to-provide-home-value-zestimate/)
- Manual assets could be entered with a name, a value and a type such as Vehicles, Valuables, Cash or Other. A search summary also says Mint could pull vehicle values from Kelley Blue Book. Note that the step-by-step flow in the same result ("Real estate, crypto, and more" > Zillow > choose Primary home, Secondary home or Rental property) appears to come from **Monarch's** help centre, not Mint's, so it must not be read as Mint's UI — [Monarch help, mixed into the search result](https://help.monarch.com/hc/en-us/articles/4404739126548-Tracking-your-property-vehicles-and-valuables)
- Yodlee supplied Mint's back-end aggregation until Intuit bought Mint in 2009. Mint then moved to Intuit's own data connections — [Search summary citing Yodlee/Mint history](https://techcrunch.com/?p=320931)
- Yodlee's generic refresh policy is about every 24 hours. Accounts with multi-factor authentication need a manual refresh. *This describes Yodlee in general, not Mint specifically* — [Yodlee docs](https://developer.yodlee.com/resources/yodlee/refresh-policy/docs); [accountsportal](https://www.accountsportal.com/docs/yodlee-bank-feeds/360019940891)
- The Investments tab could pull in 401(k)s, mutual funds, brokerage accounts and IRAs — [CNBC Select / Clark summary](https://www.cnbc.com/select/mint-budgeting-app-is-going-away-here-are-some-alternatives/)

### Inferences
- A rebuild should support these account kinds: Bank (checking, savings), Credit Card, Loan (mortgage, auto, student, personal), Investment/Retirement, Property (real estate via an AVM such as Zillow, Vehicle, Other), and Manual/Cash.
- (Unverified, from background knowledge) Mint refreshed accounts automatically each time the user logged in and about daily in the background. Users could also press a manual "Refresh" button. Connection errors (password changed, MFA required) showed as an alert icon on the account row.
- (Unverified) A manual account's balance was a value the user edited by hand. Users could add cash transactions to an "offline/cash" account.

### Gaps
- I could not read Mint's own help article on refresh frequency or on how long transaction history was imported at first link (commonly said to be about 90 days, but unverified).
- I found no confirmed list of the property sub-types Mint offered, and I could not confirm Kelley Blue Book integration from a Mint-owned source.
- I did not confirm which data providers Mint used after 2015 (Intuit's own feeds, Finicity, Plaid and others).

---

## 2. Transactions (categories, rules, splits, tags, hiding, search, export)

### Takeaway
Mint used a fixed two-level hierarchy of default categories. Users could not delete the defaults but could add custom subcategories. Users could write "always rename X and categorize as Y" rules that applied to past and future transactions, split transactions, add tags (Reimbursable and Tax Related were built in), and use a special "Hide from Budgets & Trends" category to exclude a transaction from reports.

### Cited Findings
**Category hierarchy.** This comes from a 2012 R script that hardcodes Mint's categories; I fetched it in full. Some entries are clearly that user's own custom subcategories (for example "Jeff's Allowance" and "Stacie's Allowance" under Personal Care), so the list mixes defaults with custom additions — [GitHub gist trestletech/4384914](https://gist.github.com/trestletech/4384914):
- **Auto & Transport**: Auto Insurance, Auto Payment, Gas & Fuel, Parking, Public Transportation, Service & Parts, Tolls
- **Bills & Utilities**: Home Phone, Internet, Mobile Phone, Television, Utilities (also "Cable/Internet/Phone", possibly custom)
- **Business Services**: Advertising, Legal, Office Supplies, Printing, Shipping
- **Education**: Books & Supplies, Student Loan, Tuition
- **Entertainment**: Amusement, Arts, Movies & DVDs, Music, Newspapers & Magazines
- **Fees & Charges**: ATM Fee, Bank Fee, Finance Charge, Late Fee, Service Fee, Trade Commissions
- **Financial**: Financial Advisor, Life Insurance
- **Food & Dining**: Alcohol & Bars, Coffee Shops, Fast Food, Groceries, Restaurants
- **Gifts & Donations**: Charity, Gift (also "Charity & Gifts" and "Tithe", possibly custom)
- **Health & Fitness**: Dentist, Doctor, Eyecare, Gym, Health Insurance, Pharmacy, Sports
- **Home**: Furnishings, Home Improvement, Home Insurance, Home Services, Home Supplies, Lawn & Garden, Mortgage & Rent
- **Income**: Bonus, Interest Income, Paycheck, Reimbursement, Rental Income, Returned Purchase
- **Investments**: Buy, Deposit, Dividend & Cap Gains, Sell, Withdrawal
- **Kids**: Allowance, Baby Supplies, Babysitter & Daycare, Child Support, Kids Activities, Toys
- **Loans**: Loan Fees and Charges, Loan Insurance, Loan Interest, Loan Payment, Loan Principal
- **Personal Care**: Hair, Laundry, Spa & Massage
- **Pets**: Pet Food & Supplies, Pet Grooming, Veterinary
- **Shopping**: Books, Clothing, Electronics & Software, Hobbies, Sporting Goods
- **Taxes**: Federal Tax, Local Tax, Property Tax, Sales Tax, State Tax
- **Transfer**: Credit Card Payment, Transfer for Cash Spending
- **Travel**: Air Travel, Hotel, Rental Car & Taxi, Vacation
- **Uncategorized**: Cash & ATM, Check

**Other category findings**
- An independent search summary confirms the same Auto & Transport subcategories (Auto Insurance, Auto Payment, Gas & Fuel, Parking, Public Transportation, Service & Parts, Tolls). It also says Mint did not let users remove default categories or subcategories, but they could add and remove custom subcategories — [schrauger GitHub addon](https://github.com/schrauger/mint.com-customize-default-categories); [gist](https://gist.github.com/trestletech/4384914)
- Hiding default categories was so commonly requested that a third-party browser add-on was built to hide them on the client side. The add-on stored its settings in "3 custom category fields" under Uncategorized. This shows that custom subcategories were saved on the server and attached to a parent category — [schrauger/mint.com-customize-default-categories](https://github.com/schrauger/mint.com-customize-default-categories)
- A special category, **"Hide from Budgets & Trends"**, kept a transaction out of both budgets and trends. It was used for transfers and for a purchase plus its refund — [Ford Financial Solutions, "Best practices for using Mint" (2017)](https://www.fordfinancialsolutions.com/blog/2017/02/07/best-practices-for-using-mint); [YouTube: Best Practices For Using Mint: Hide from Budgets & Trends](https://www.youtube.com/watch?v=SHFliqkkqIE)
- Mint offered a downloadable list of every category and subcategory "as of July 2023" (PDF and Excel). I could not fetch it — [couragepassion blog (blocked)](https://couragepassion.wordpress.com/2023/08/02/mint-app-categories-and-subcategories-%F0%9F%93%84/)

**Rules and renaming**
- Users could create naming and category rules, and Mint applied each rule to **all future and past** transactions. On the web: Transactions > select a transaction > Edit Details > change the description or category > a RULES area appears > tick "Always rename [payee] and categorize as [category]". Rules were managed under "Manage your transaction rules" at the bottom of the Transactions page. On Android, after renaming or recategorizing, Mint asked whether to "Apply to all transactions" — [help.mint.com: How do I create, view, and delete transaction rules?](https://help.mint.com/Accounts-and-Transactions/888961281/How-do-I-create-view-and-delete-transaction-rules.htm)

**Splits**
- Users could split one transaction into several categories, for example one store visit that covers several budgets. The mobile app gained splits in May 2012 — [TechCrunch 2012](https://techcrunch.com/2012/05/23/mint-com-app-update-now-split-transactions); [help/search summary](https://www.thefinancialfreedomproject.com/laying-your-financial-foundation-a-guide-to-mint-transactions/)

**Tags**
- "Reimbursable" and "Tax Related" were built-in default tags. Users created new tags from the Transaction Details panel or from the tags section in the left sidebar of the Transactions page. Tags group transactions across categories for filtering. Tags such as "Vacation" and "Business" appear in the sources only as tags users made themselves — [Smart Sister Finance](https://smartsisterfinance.com/resources/how-to-get-the-most-out-of-mintcom); [Financial Freedom Project](https://www.thefinancialfreedomproject.com/laying-your-financial-foundation-a-guide-to-mint-transactions/)

### Inferences
- (Unverified, background knowledge) The default tags were probably **Reimbursable, Tax Related, Vacation and Business**. The sources I could reach confirm only the first two.
- (Unverified) Later category lists, around 2020 to 2023, probably added items such as "Food Delivery", "Misc Expenses", "Hide from Budgets & Trends" as a top-level category, and "Mortgage Payment" under Transfer. Check these against the July 2023 list.
- (Unverified) Other transaction behaviours worth checking before reproducing them:
  - **Pending transactions** were shown greyed out and italic and could not be edited until posted.
  - **Duplicates** could be flagged with "Mark as duplicate" (hidden but kept).
  - **Cash and manual transactions** could be added with "+ Add transaction", with the option to deduct from cash.
  - **Notes** were a free-text field in Edit Details.
  - **Search** supported text plus filters by account, category, tag and date.
  - **CSV export** was a link at the bottom of the transaction list that exported the current filtered view, with columns Date, Description, Original Description, Amount, Transaction Type (debit/credit), Category, Account Name, Labels and Notes.

### Gaps
- I could not get the authoritative 2015 to 2024 default category list (the fetchable gist dates from 2012).
- I found no sourced detail on how duplicates or pending transactions were handled, or on the CSV export columns.
- I could not confirm Vacation and Business as default tags.

---

## 3. Budgets

### Takeaway
Budgets were monthly by default. Users could also set budgets for expenses that happened every few months or once a year. A per-budget rollover option carried leftover money into the next month, with a reset-to-$0 option. Progress bars were coloured green (on track), yellow (spending faster than the month allows, close to the limit) and red (over budget).

### Cited Findings
- To turn on rollover: Budgets > hover over the budget > Edit Details > tick "Start each new month with the previous month's leftover amount". There was also a "Reset rollover to $0" option that cleared the rollover for that month without turning the feature off — [Annielytics, via search summary](https://www.annielytics.com/blog/personal/8-ways-customize-mint-make-budgeting-easier/)
- Budget frequency could be set for intervals other than monthly, such as quarterly or yearly expenses (spread over the months) — [juvoni.com: Using Mint.com for Monthly and Annual Budgeting](https://juvoni.com/using-mint-com-for-monthly-and-annual-budgeting/)
- Colour meanings: green means on track, yellow means close to going over or spending faster than the month allows (for example, more than half the budget spent at the halfway point of the month), and red means over budget. The point where the bar turns from green to yellow marks where spending got ahead of the budget — [A. Chang, "The 4 Step Quickstart to Budgeting on Mint" (Medium)](https://amchang.medium.com/the-4-step-quickstart-to-budgeting-on-mint-dbc272c9d02); [Diamondback review 2021](https://dbknews.com/2021/09/21/mint-budget-and-expense-tracker-app-financial-spending-habits/)
- The 2021 app's insights suggested budget changes "to save more based on your historical and current spending" — [Intuit press release, Jan 2021](https://investors.intuit.com/news-events/press-releases/detail/196/all-new-mint-ios-app-launched-to-empower-financial-freedom)
- Credit Karma, Mint's replacement, did not keep category budgets or customized categories — [NerdWallet (search summary)](https://www.nerdwallet.com/article/finance/mint-app-closing-what-it-means-how-to-pick-a-new-budget-service); [Rocket Money](https://www.rocketmoney.com/learn/personal-finance/mint-app-shutting-down)

### Inferences
- The pacing behaviour is consistent with a "budget-to-date" amount of budget × (days elapsed / days in month). Yellow appears when spent is above that pace (or near the limit), and red when spent is above the budget.
- (Unverified, background knowledge) Further budget behaviours to confirm before rebuilding:
  - **"Everything Else" budget.** A single catch-all budget covering every category that had no budget of its own.
  - **Income budgets.** These appeared in a separate income section with "earned vs budgeted".
  - **Spent and remaining.** Spent was the sum of non-hidden transactions in the category (and its subcategories) for the month, and remaining was budget + rollover − spent.
  - **Auto-suggested budgets.** At first setup, Mint auto-created suggested budgets from about three months of history.
  - **Annual/infrequent budgets.** These sat in a separate "spending/saving for later" area.

### Gaps
- I found no Mint help text that confirms how "Everything Else" worked, how income budgets were calculated, the exact yellow threshold, or how a parent category budget interacts with subcategory budgets (whether a parent-category budget includes its subcategories' spending).

---

## 4. Trends

### Takeaway
Trends offered reports for spending (by category, merchant or tag, as a pie chart or as bars over time), income, net income over time, and net worth over time (assets vs debts). Spending and Income reports filtered by time, category, merchant and account; Net Income Over Time filtered only by time.

### Cited Findings
- Trends tracked spending, income and net worth over time. Reports could be organised by category, merchant, account, date or tags — [Retail Dive / Mobile Commerce Daily, "Mint adds Trends to mobile app"](https://www.retaildive.com/ex/mobilecommercedaily/mint-adds-%25E2%2580%259Ctrends%25E2%2580%259D-to-mobile-app-gives-financial-perspective-to-consumers)
- The report tabs were: Spending by Category, Spending by Merchant, Spending Over Time, Net Income Over Time (income and spending together), and Net Worth Over Time (total, assets and liabilities, grouped by account). Spending and Income reports could be filtered by time, category, merchant and account; Net Income Over Time could be filtered only by time — [The Golden Albatross, "Mint (All) Breakdown"](https://grumpusmaximus.com/track-money-part-4-mint-breakdown/); [Tiller community](https://community.tiller.com/t/spending-and-income-dashboards-for-mint-trends-lovers/21892)
- The 2021 iOS redesign added "an advanced month-over-month view of net worth and spending" — [Intuit press release Jan 2021](https://investors.intuit.com/news-events/press-releases/detail/196/all-new-mint-ios-app-launched-to-empower-financial-freedom)

### Inferences
- (Unverified) Time ranges were probably presets (This month, Last month, Last 3 months, This year, Last year, All time) plus a custom range. Clicking a pie slice or bar probably drilled into its subcategories and then into the matching transactions.

### Gaps
- I could not confirm the exact preset time ranges or the chart types (pie vs donut) from help docs.

---

## 5. Net worth and investments

### Takeaway
An Investments tab gathered 401(k), IRA, brokerage and mutual-fund accounts, showed allocation, compared performance to market benchmarks and pointed out fees. Reviewers considered it basic. Net worth was assets (including property) minus debts.

### Cited Findings
- Mint's portfolio and investment tracking covered 401(k)s and other accounts and "keeps you informed of any fees you're being charged". Reviewers called the investing area "very weak for all but the basic investors" — [SmartAsset](https://smartasset.com/financial-advisor/mint-review); [Rockstar Finance](https://rockstarfinance.com/mint-app-review)
- Users could compare their portfolio to market benchmarks but could not trade or manage investments — [Clark.com](https://clark.com/save-money/mint-review/); [CNBC Select](https://www.cnbc.com/select/mint-budgeting-app-is-going-away-here-are-some-alternatives/)
- Net Worth Over Time showed total, assets and liabilities by account — [Golden Albatross](https://grumpusmaximus.com/track-money-part-4-mint-breakdown/)
- Net worth tracking moved to Credit Karma after Mint closed — [Rocket Money](https://www.rocketmoney.com/learn/personal-finance/mint-app-shutting-down)

### Inferences
- (Unverified) The benchmarks were probably the S&P 500, the Dow and the NASDAQ, and allocation was probably shown by asset class (stocks, bonds, cash and so on) as a pie chart.

### Gaps
- I found no sourced detail on how the benchmark comparison or the fee analysis was calculated.

---

## 6. Bills (Check acquisition, bill pay)

### Takeaway
Intuit bought the bill app Check (formerly Pageonce) for $360M in May 2014 and renamed it **Mint Bills** in December 2014. Bill pay was folded into Mint.com in March 2017 and shut down on 30 June 2018 because few people used it. Bill tracking and reminders carried on in Mint until the 2024 shutdown.

### Cited Findings
- May 2014: Intuit agreed to buy Check for $360M. December 2014: Check apps became Mint Bills. Check was earlier called Pageonce — [Wikipedia (search summary)](https://en.wikipedia.org/wiki/Intuit_Mint); [LinkedIn Check Inc. (now Mint Bills)](https://www.linkedin.com/company/mintbills); [FinAi News: "Mint Finally Adds Bill Pay - in a Separate App"](https://bankautomationnews.com/allposts/retail/mint-finally-adds-bill-pay-in-a-separate-app/)
- Bill pay and bill management launched on Mint after the competitor Manilla closed (Finovate) — [Finovate](https://finovate.com/after-manillas-demise-mint-launches-bill-pay-and-bill-management-platform/)
- Mint Bills was integrated into Mint.com in March 2017, and bill payment was discontinued on 30 June 2018 because of low adoption — [Bank Automation News: "Mint Shutters Bill Payment Feature"](https://bankautomationnews.com/allposts/payments/mint-shutters-bill-payment-feature/); [CCG Catalyst](https://ccginsights.com/insight/the-end-of-bill-pay)
- Mint alerted users when bills were due — [mint.intuit.com/how-mint-works/alerts (search summary)](https://mint.intuit.com/how-mint-works/alerts)
- Credit Karma did not keep the view of upcoming bills — [Rocket Money](https://www.rocketmoney.com/learn/personal-finance/mint-app-shutting-down)

### Inferences
- (Unverified) Bills could be pulled from linked billers (pulling amount due and due date) or added manually as recurring bills. Reminders went out a set number of days before the due date.

### Gaps
- I could not confirm the reminder lead times or how bills were detected automatically.

---

## 7. Goals

### Takeaway
Goals offered eight preset types. Each goal could be linked to a specific account, so progress updated whenever that account's balance changed, and Mint calculated the monthly saving needed to hit the target date.

### Cited Findings
- The eight preset goals were: get out of debt, save for an emergency, save for retirement, buy a home, buy a car, save for college, take a trip, and improve my home — [Money Crashers review (search summary)](https://www.moneycrashers.com/mint-review/); [Bible Money Matters](https://www.biblemoneymatters.com/review-of-mints-new-goals-feature/); [TheStreet: Mint launches savings goal tool](https://www.thestreet.com/personal-finance/mintcom-launches-savings-goal-tool-12806610)
- Goals were linked to specific accounts, so progress updated with the account balance. Mint calculated the required monthly contribution. The "buy a car" flow asked whether the user knew the cost or wanted an estimate, whether they would pay cash or finance, the trade-in value and the deadline — [Money Crashers](https://www.moneycrashers.com/mint-review/)
- For a debt payoff goal, Mint tracked payments and progress and estimated a completion date — [Student Loan Sherpa](https://studentloansherpa.com/mint-com-pay-student-loans/)
- Credit Karma did not keep savings goal tracking — [Rocket Money](https://www.rocketmoney.com/learn/personal-finance/mint-app-shutting-down)

### Inferences
- (Unverified) There was probably also a custom goal type. The monthly amount needed was probably (target − current) / months remaining, and goal contributions may have shown up as a line in the budget.

### Gaps
- I could not confirm the custom goal type or how goal amounts were treated inside budgets.

---

## 8. Alerts and notifications

### Takeaway
Alerts covered unusual or large spending, low balances, upcoming bills, going over budget, fees, credit score changes and (from 2021) subscription price changes. Weekly or monthly summary emails covered balances, budgets, investments and goals.

### Cited Findings
- Mint's alerts page listed alerts for unusual or large spending, low balances and bills due, plus budget alerts and weekly summaries — [mint.intuit.com/how-mint-works/alerts](https://mint.intuit.com/how-mint-works/alerts)
- Weekly or monthly summary emails covered balances, budgets and stocks, and included bill reminders, credit score updates and spending alerts. Financial advice emails were also available — [Clark.com](https://clark.com/save-money/mint-review/); [MakeUseOf](https://www.makeuseof.com/tag/7-advanced-tips-and-tricks-to-make-you-a-mint-expert/)
- The mobile app gained a weekly summary page in 2015, alongside iOS 9 support — [iClarified](https://www.iclarified.com/51724/mint-money-app-gets-ios-9-support-weekly-summary-page)
- 2021: users could set an alert for when a subscription's price went up or down, and insights alerted users when bills increased — [Intuit press release Jan 2021](https://investors.intuit.com/news-events/press-releases/detail/196/all-new-mint-ios-app-launched-to-empower-financial-freedom)

### Inferences
- (Unverified) The low-balance and large-purchase thresholds could probably be set per account, and each alert type could probably be sent by email, push or SMS.

### Gaps
- I could not find the default thresholds, which delivery channels each alert supported, or how "unusual spending" was detected.

---

## 9. Credit score

### Takeaway
From 2 October 2017 Mint showed a free **VantageScore 3.0 from TransUnion**, updated about monthly. A paid credit monitoring service was also offered.

### Cited Findings
- From 2 October 2017 Mint used TransUnion data and the VantageScore 3.0 model, updated monthly — [Doctor of Credit](https://www.doctorofcredit.com/mint-com-review-free-credit-score-report-monitoring/)
- A paid credit monitoring add-on was quoted at $16.99/month — [SmartAsset](https://smartasset.com/financial-advisor/mint-review)
- Credit score updates appeared in the summary emails — [Clark.com](https://clark.com/save-money/mint-review/)

### Inferences
- (Unverified, background knowledge) Before October 2017 the free score likely came from Equifax (launched around 2013). The credit factors screen probably showed: payment history, credit card utilization, age of credit, total accounts, hard inquiries, and derogatory marks.

### Gaps
- I could not confirm the pre-2017 provider or the exact credit factors and their grade thresholds.

---

## 10. Other features and timeline (Ways to Save, insights, subscriptions, Premium, shutdown)

### Takeaway
Mint made its money from "Ways to Save" referral offers. The January 2021 redesign added subscription tracking and "Mintsights". Mint Premium ($4.99/month, iOS only) added ad removal plus bill negotiation and cancellation through Billshark, and a separate ad-free tier cost $0.99/month. Intuit announced the shutdown in November 2023 and Mint closed on 23 March 2024, with users moved to Credit Karma.

### Cited Findings
- "Ways to Save" recommended accounts, cards and services and earned Mint referral fees, with projected savings from switching providers — [Clark.com](https://clark.com/save-money/mint-review/); [MakeUseOf](https://www.makeuseof.com/tag/7-advanced-tips-and-tricks-to-make-you-a-mint-expert/)
- The January 2021 redesign brought subscription monitoring, a month-over-month view of net worth and spending, and personalised "Mintsights™" — [Intuit press release](https://investors.intuit.com/news-events/press-releases/detail/196/all-new-mint-ios-app-launched-to-empower-financial-freedom); [Business Wire](https://www.businesswire.com/news/home/20210106005476/en/All-New-Mint-iOS-App-Launched-to-Empower-Financial-Freedom); [CardRates](https://www.cardrates.com/news/updated-mint-app-provides-a-holistic-financial-view/)
- Mint Premium cost $4.99/month and was iOS-only. It included ad removal, bill negotiation and cancellation through Billshark, and spending projections and comparisons with others. A separate ad-free tier cost $0.99/month — [Retirely / Motley Fool, "Is Mint's Premium Subscription Worth It?" (2023)](https://retire.ly/2023/06/29/is-mints-premium-subscription-worth-it/); [Moneywise](https://moneywise.com/managing-money/budgeting/mint-com)
- Intuit announced in early November 2023 that Mint would close. The date was first set for 1 January 2024 and moved to 23 March 2024, and users were moved to Credit Karma — [Bloomberg](https://www.bloomberg.com/news/articles/2023-11-01/intuit-winds-down-personal-finance-app-mint-shifts-users-to-credit-karma); [WalletHub](https://wallethub.com/edu/b/what-happened-to-mint/151868); [Rocket Money](https://www.rocketmoney.com/learn/personal-finance/mint-app-shutting-down)
- What moved to Credit Karma: linked accounts, transaction history and net worth. What did not: budgets, custom categories, bill reminders and goals — [Rocket Money](https://www.rocketmoney.com/learn/personal-finance/mint-app-shutting-down); [Vento](https://vento.money/blog/mint-shutdown-what-now/)
- Mint's former first product manager, now CEO of Monarch, wrote about the shutdown — [Monarch blog](https://www.monarch.com/blog/mint-shutting-down)

### Inferences
- Rough timeline for a rebuild:
  - **2009**: Intuit acquires Mint; Yodlee replaced.
  - **2010**: Zillow integration.
  - **2012**: Mobile split transactions.
  - **2014**: Check acquisition becomes Mint Bills.
  - **2015**: Weekly summary in the app.
  - **March 2017**: Bill pay integrated into Mint.com.
  - **October 2017**: TransUnion VantageScore 3.0.
  - **June 2018**: Bill pay removed.
  - **January 2021**: Redesign with subscriptions and Mintsights.
  - **About 2021 to 2023**: Mint Premium (iOS).
  - **November 2023**: Shutdown announced.
  - **23 March 2024**: Mint closed.

### Gaps
- I could not confirm the launch date of Mint Premium (the prompt suggests 2022, but no source confirms it).
- I found no source for the "Mint Minute" branding or for a "financial health" score feature.
- I could not confirm whether Premium ever reached Android or the web.
