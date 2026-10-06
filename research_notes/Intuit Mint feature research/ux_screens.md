# Intuit Mint UX, Information Architecture and Visual Design (web and iOS, c. 2015-2024)

> **Method note (please read first):** Every attempt to fetch a full page was blocked by the network egress proxy. The blocked domains were medium.com, investors.intuit.com, pcworld.com, tomsguide.com, cardrates.com, afb.org, applevis.com, ixd.prattsi.org and en.wikipedia.org. So every finding below comes from **search-engine result snippets and summaries**, not from reading the pages. Treat the details as "reported by the source as summarised in search". The report writer should be careful about exact wording. Screen-by-screen detail (exact tab labels per version, pixel layout, hex colours) could not be checked against screenshots, and those items are listed as Gaps.
>
> **Warning on contaminated results:** One search for the budget "today" pace line returned GitHub pull requests from unrelated hobby budget apps (rod-feuer/daybook, rickychongjl/budget-app). The search summary wrongly blended them into a description of Mint, for example "a line taller than the bar, ringed in the card color" and "Spend fills in the accent at 60%; only the overage is red". **Those details are NOT Mint sources and must not be attributed to Mint.**

## iOS app navigation and the Overview/home screen (pre-2020 and the 2020-2021 redesign)

### Takeaway
The pre-redesign iOS app (c. 2015-2019) opened on a scrolling Overview. It showed cash and credit totals, recent earned and spent, a spending pie chart, alerts, an "Advice" (offers) entry, and modules for credit score and cash flow. The redesign ("All New Mint", rolled out on iPhone in late 2020 and announced widely in January 2021) moved Net Worth and Credit Score into a fixed header. It added a "This Month" tab (spending, trends and budgets), a swipeable carousel of "MintSights" insight cards and automatic subscription tracking.

### Cited Findings
- **Older Overview screen (AFB AccessWorld evaluation, URL path suggests 2014, vol. 15 no. 4):** after login you see an overview screen with your combined cash on hand and your total outstanding credit. Below that is a summary of your recent earned and recent spent amounts. — [AFB AccessWorld](https://afb.org/aw/15/4/15583)
- Near the top of the Overview screen is a button labelled "Accounts". Below the totals is a button for alerts, then a button for "Advice", which "tends to be sales pitches for various credit cards and savings accounts". — [AFB AccessWorld](https://afb.org/aw/15/4/15583)
- Near the bottom of the summary screen is a pie chart that groups recent spending by category. The control can be swiped up or down with one finger, but the chart itself was not accessible to VoiceOver. — [AFB AccessWorld](https://afb.org/aw/15/4/15583)
- **2017 design critique (Pratt IXD, Jan 31 2017):** it describes the main functions visible when you open the app as clear sections, such as **credit scores, cash flow and a chart of spending**. — [Pratt IXD Design Critique: Mint (iOS app)](https://ixd.prattsi.org/2017/01/design-critique-mint-iphone-app/)
- A MakeUseOf article says Mint "completely redesigned its iOS apps" around the middle of a year in the early 2010s, giving "a simpler way to view their financial accounts, transactions, and budgets". The exact year is unclear from the snippet. — [MakeUseOf](https://www.makeuseof.com/tag/mint-budget-spending-online/)
- **2020-2021 redesign:** Intuit launched "a significantly updated Mint experience" with "a major design refresh", subscription monitoring, "an advanced month-over-month view of net worth and spending" and personalised "Mintsights". It was rolled out on iPhone first, with Android and web "in the coming months". — [Intuit press release "Mint Reinvents App Experience"](https://investors.intuit.com/news-events/press-releases/detail/315/mint-reinvents-app-experience-paving-the-way-to-a-better-money-management-solution); also [Business Wire, 6 Jan 2021](https://www.businesswire.com/news/home/20210106005476/en/)
- Redesign: users "would no longer have to scroll to access critical financial information such as: notifications, credit score, and accounts". **"Net Worth & Credit Score now have dedicated real estate in the header of the app."** — [Intuit press release "All New Mint iOS App Launched"](https://investors.intuit.com/news-events/press-releases/detail/196/all-new-mint-ios-app-launched-to-empower-financial-freedom)
- Redesign: **a new "This Month" tab** "provides immediate access to all your spending, trends, and budgets with a single tap". — [Intuit press release](https://investors.intuit.com/news-events/press-releases/detail/196/all-new-mint-ios-app-launched-to-empower-financial-freedom)
- Redesign: **MintSights** are "data-driven insights that highlight the most important aspects of your finances, presenting easily digestible text on large organized cards with simple context below". You "swipe right through the carousel to see your most important MintSights for the day". — [Intuit press release](https://investors.intuit.com/news-events/press-releases/detail/196/all-new-mint-ios-app-launched-to-empower-financial-freedom); [WealthManagement.com](https://www.wealthmanagement.com/financial-technology/mint-launches-mintsights-moves-closer-to-providing-financial-advice)
- MintSights covered "10 unique situations, from creating a first budget to debt consolidation to growing investments". They also surface "unbeatable offers from Mint's trusted partners", which means offers were built into the insight cards. — [Intuit press release "Mint Reinvents…"](https://investors.intuit.com/news-events/press-releases/detail/315/mint-reinvents-app-experience-paving-the-way-to-a-better-money-management-solution)
- Example insights: alerting you "when your bills increase" or suggesting "opportunities to adjust your budgets to save more based on your historical and current spending". — [Intuit press release](https://investors.intuit.com/news-events/press-releases/detail/196/all-new-mint-ios-app-launched-to-empower-financial-freedom)
- Redesign added "trend visualizations" on the overview, showing how net worth changes over time and month-over-month spending. — [Intuit press release](https://investors.intuit.com/news-events/press-releases/detail/196/all-new-mint-ios-app-launched-to-empower-financial-freedom)
- Subscriptions: the app automatically tracked subscription costs using its own algorithms and alerted users when a subscription went up or down in price. — [Intuit press release](https://investors.intuit.com/news-events/press-releases/detail/315/mint-reinvents-app-experience-paving-the-way-to-a-better-money-management-solution)
- At the time of the redesign Mint said it had "more than 10 million active annual users". — [Intuit press release](https://investors.intuit.com/news-events/press-releases/detail/315/mint-reinvents-app-experience-paving-the-way-to-a-better-money-management-solution)
- Fast Company covered the redesign as "Intuit's plan to get Mint back on track after years of neglect". — [Fast Company](https://www.fastcompany.com/90591464/intuit-mint-new-version-and-alternatives)
- A Medium post by Alex Valaitis, "Introducing the New Mint", describes "the latest refresh of Mint for iOS". He appears to have been involved with the product, but this could not be read. — [Medium](https://medium.com/@Alex.Valaitis/introducing-the-new-mint-3aa44f2c0313)
- Account adding (help docs): open the app, **tap the plus sign in the top right corner**, choose Add Account, search for the institution and enter your bank credentials. — [Further Learning / help guide](https://learn.hellofurther.com/Individuals/Profile_and_Settings/How_to_Add_Your_Accounts_to_Mint); [Mint help](https://help.mint.com/General/888961131/How-do-I-add-a-new-account-in-Mint.htm)
- An older help path for splitting refers to "Updates > All Transactions (under Recent Transactions)". This suggests that a home or overview screen was called **"Updates"** in some version, with a "Recent Transactions" module and an "All Transactions" link. — [Mint community Q&A](https://mint.lc.intuit.com/questions/1018176-how-do-i-split-transactions-in-my-iphone-app)

### Inferences
- Order of home modules before the redesign (c. 2014-2017, from AFB and Pratt): Accounts button, then cash and credit totals, then recent earned and spent, then alerts, then Advice (offers), then a spending-by-category pie chart. Credit score and cash flow modules appear by 2017. This order is pieced together from two sources and is not confirmed by screenshots.
- Post-redesign structure (2020/21 to 2024): a persistent header with Net Worth and Credit Score (and probably a notifications bell). A home feed with a MintSights card carousel and trend charts. A "This Month" tab that combines spending, trends and budgets. Account adding sits behind a "+" at the top right.
- "Updates" was probably a tab or home label in the later bottom-tab app, but this is unconfirmed.

### Gaps
- I could not confirm the exact bottom-tab labels for any version. The commonly cited sets ("Overview / Transactions / Budgets / Trends / More" before the redesign, and something like "Overview / This Month / Transactions / … / More" after it) could not be checked because App Store screenshots and the Wayback Machine could not be fetched. One search aimed at a 2016-2017 tab bar returned nothing definitive.
- I could not confirm whether the pre-2016 app used a hamburger (side drawer) menu or a tab bar.
- I found no source on "Mint Minute". It may be a product name the user remembers, or it may not exist. No reference was found.
- I found no source on bills-due module placement within the post-2020 home feed.

## Web app tabs and layout

### Takeaway
On the web app the main tabs (Overview, Transactions, Budgets, Goals, Trends, Ways to Save, plus Investments) ran across the top right. The Overview page had an accounts sidebar on the left. Overview was the "home base", with balances and alerts. Transactions was a day-by-day editable list. Budgets were per-category bars. Trends was a charting area. Ways to Save was the offers marketplace.

### Cited Findings
- "All of the tabs such as Overview, Transactions, Budgets, Goals, etc., are in the upper right-hand corner, with account information on the left sidebar." — [search summary of Mint getting-started guides, e.g. Get Rich Slowly / MakeUseOf / Moneywise](https://www.getrichslowly.org/getting-started-with-mint/)
- Overview: "a Big Picture view of your finances, including account balances and alerts", meant to be "your home base in Mint". — [Moneywise get-started guide](https://moneywise.com/managing-money/budgeting/get-started-mint)
- Transactions: "a day-by-day list of your income and expenses, where you can view and edit each of your purchases and deposits". — [MakeUseOf beginner's guide](https://www.makeuseof.com/tag/a-beginners-guide-to-managing-your-money-with-mint/)
- Budgets: set budgets per category (groceries, entertainment, transportation) on a monthly or yearly basis. Mint tracks spending and shows whether you are over or under. — [Mint Activate: Budgeting tab](https://www.mintactivate.com/navigating-the-app-using-the-budgeting-tab)
- Goals: set savings goals, and Mint "walks you through the process of creating a realistic budget for each goal". — [Money Crashers](https://www.moneycrashers.com/mint-review/)
- Trends: "your headquarters for financial analysis", with bar graphs or pie charts of spending, income, net income, assets, debts and net worth over customisable periods. — [Money Crashers](https://www.moneycrashers.com/mint-review/)
- Ways to Save: cash-back and travel credit cards, bank accounts, brokerage accounts, insurance, 401(k) rollovers and loans. — [Money Crashers](https://www.moneycrashers.com/mint-review/)
- Budget bars appear "on your Budget and Overview tabs". — [PCWorld review](https://www.pcworld.com/article/407663/mint-review-financial-budgeting-app.html)
- Web onboarding: the process first asks for country and zip code. It then prompts you to connect your first bank, with "trust-building copy that describes the 3 steps" and recognisable bank logos. — [Appcues GoodUX](https://goodux.appcues.com/blog/mint-user-onboarding-product-tour)

### Inferences
- A Mint-like web layout uses a top bar with a logo on the left and tabs on the right, a left column with grouped account balances (Cash, Credit Cards, Loans, Investments, Property) and a main column with alerts, budget bars and offers.

### Gaps
- I found no source describing the Investments tab layout.
- No exact pixel or column layout was confirmed from screenshots.

## Transactions list: row anatomy, category editing, splits

### Takeaway
Transactions could be edited per row (category, description, exclude). Split transactions came to iPhone in 2012: you open a cleared transaction, tap "Split" at the top right, add line items each with a category and amount, then Save.

### Cited Findings
- TechCrunch (23 May 2012): a Mint app update added split transactions. — [TechCrunch](https://techcrunch.com/2012/05/23/mint-com-app-update-now-split-transactions)
- iPhone split flow: open the account, then the transaction, and tap the **blue "Split" link in the upper right corner**. Only cleared (not pending) transactions can be split. Another path: Updates > All Transactions > tap the transaction > Split > "+ Add" > tap the new line item > choose a category > enter the amount > Save. — [Mint community Q&A](https://mint.lc.intuit.com/questions/1018176-how-do-i-split-transactions-in-my-iphone-app)
- After splitting you can edit the category on each split, or exclude it. — [Mint community Q&A](https://mint.lc.intuit.com/questions/1012307-how-do-i-designate-one-transaction-to-two-categories)

### Inferences
- The blue "Split" link suggests that iOS-default blue tint was used for navigation-bar actions in that era, rather than Mint green. This is a weak inference.

### Gaps
- I could not confirm the row anatomy: date column or section headers, merchant name, category text or icon, amount colour (for example green for income), or how pending items were styled. Screenshots could not be fetched.
- I could not confirm whether the category picker was a two-level list (parent category, then subcategory). This is widely remembered, but I found no source in this session.

## Budget screen: progress bars, colours, pacing line, adding a budget

### Takeaway
Each budget was a horizontal bar. It stayed green while within budget, turned yellow as it approached the limit and turned red when over. Budgets could be monthly or yearly per category. Adding a budget was category-based. The redesign added budget-adjustment suggestions through MintSights.

### Cited Findings
- "Each budget is displayed as a bar graph on your Budget and Overview tabs, with the bar remaining green when you're within budget but turning yellow as you approach the limit and red when you're over budget." — [PCWorld review](https://www.pcworld.com/article/407663/mint-review-financial-budgeting-app.html)
- Green means on track, yellow means "almost exceeding" and red means over budget. — [Marble Jar tutorial, 2018](http://www.marblejar.net/2018/06/my-favorite-budgeting-app-mintcom.html)
- Budgets were set per category, monthly or yearly. — [Mint Activate](https://www.mintactivate.com/navigating-the-app-using-the-budgeting-tab)
- The Apple Watch app had a discretionary daily spending budget showing "how much you can spend today". — [Wise Bread](https://www.wisebread.com/8-apple-watch-apps-thatll-save-you-money)
- An App Store listing description mentions a "daily budget planner" that suggests budget goals based on your spending. — [CNBC Select review / App Store](https://www.cnbc.com/select/mint-budgeting-app-review/)

### Inferences
- A pacing or "today" marker on Mint budget bars is commonly remembered, but **I found no Mint source for it** (see the contamination warning at the top).

### Gaps
- No Mint-specific source was found for the "today" pacing line, its look or the exact thresholds for yellow.
- The step-by-step add-budget flow on iOS (category picker, amount, recurrence, rollover) was not confirmed.

## Trends: chart styles and drill-down

### Takeaway
Web Trends offered pie and bar charts for spending, income, net income, assets, debts and net worth. It had hover tooltips, click-to-drill into slices and filters for accounts, time ranges and search. The 2021 redesign brought month-over-month net worth and spending trends into the app overview and the "This Month" tab.

### Cited Findings
- Users typically start with a "Spending by Category" pie chart. Some graphs can switch to a pie chart through a control above the graphs on the left side. — [My Money Blog](https://www.mymoneyblog.com/mint-com-tips-tricks.html); [MakeUseOf advanced tips](https://www.makeuseof.com/tag/7-advanced-tips-and-tricks-to-make-you-a-mint-expert/)
- Hovering over a pie segment shows a pop-up with the total spent and the number of transactions. Clicking a slice lets you "dive in" to that category's transactions. — [MakeUseOf / Mint tips summaries](https://www.makeuseof.com/tag/7-advanced-tips-and-tricks-to-make-you-a-mint-expert/)
- Filters above the chart let you choose accounts, time intervals and search for transactions or merchants. — [My Money Blog](https://www.mymoneyblog.com/mint-com-tips-tricks.html)
- Trends options covered spending, income, net income, assets, debts and net worth "over customizable periods". — [Money Crashers](https://www.moneycrashers.com/mint-review/)
- The Tiller community built "Spending and Income Dashboards for Mint Trends Lovers", which suggests users valued Trends after Mint shut down. — [Tiller Community](https://community.tiller.com/t/spending-and-income-dashboards-for-mint-trends-lovers/21892)

### Gaps
- The iOS-specific Trends chart styles (for example donut versus bar, or month selector chips) were not confirmed.

## Onboarding and first-run experience

### Takeaway
Web onboarding was trust-led: country and zip, then a three-step explanation, then connecting a bank with recognisable logos. On mobile, the first-run experience reportedly dropped users straight into the app after the first account connected, with no tour.

### Cited Findings
- Web: select country and enter zip code, then a prompt to connect the first bank account. It uses trust-building copy describing the three steps and shows popular bank logos to ease security fears. — [Appcues GoodUX](https://goodux.appcues.com/blog/mint-user-onboarding-product-tour)
- Mobile: "After the app finishes connecting the first bank account on mobile, users are taken straight into the app with no tour, no tooltips, and nothing to guide users on what they should do next." — [Appcues GoodUX](https://goodux.appcues.com/blog/mint-user-onboarding-product-tour)
- Add account: tap "+" at the top right, then Add Account, search for the institution and enter credentials. — [Further Learning help](https://learn.hellofurther.com/Individuals/Profile_and_Settings/How_to_Add_Your_Accounts_to_Mint)
- A Medium UX case study (not an official Mint source) describes a persona discovering she is a "reactive reducer" after Mint's onboarding. This hints at a money-personality quiz in some onboarding version. — [Medium case study](https://medium.com/@abigailabbyabilvera79/ui-ux-case-study-mint-the-budget-app-e99c971439d5)

### Gaps
- No dated source for the post-2020 iOS onboarding screens. The "reactive reducer" personality step is unverified from Intuit sources.

## Visual identity: logo, palette, typography, tone

### Takeaway
I found almost no citable material on Mint's brand system in this session.

### Cited Findings
- Reviews describe Mint's "colorful graphics" for automatic categorisation. — [Listen Money Matters / review summaries](https://www.listenmoneymatters.com/mint-review/)
- The brand was later styled "Intuit Mint" (for example the Tom's Guide 2022 review title "Intuit Mint review"). — [Tom's Guide](https://www.tomsguide.com/reviews/intuit-mint-2022)
- The current mint.intuit.com page says Mint has been "reimagined on Credit Karma". — [mint.intuit.com](https://mint.intuit.com/)

### Gaps
- No source found for the leaf logo's history, hex values, typeface or tone-of-voice guidelines. Logo searches returned Linux Mint and other unrelated brands. The logos-world.net page on Mint was found but not fetchable, and it may also refer to a different Mint.

## Notifications, widgets, security, Apple Watch

### Takeaway
Mint offered a 4-digit passcode and Touch ID (Face ID presumably later), and an Apple Watch app focused on a daily discretionary "safe to spend" budget. It sent alerts for bills, subscription price changes and similar events. A net worth widget is mentioned only weakly.

### Cited Findings
- "Mint will require a 4-digit code to view your account or Touch ID." — [Moneywise Mint.com review](https://moneywise.com/managing-money/budgeting/mint-com)
- The app listing says it is compatible with iPhone, iPad, iPod touch and Apple Watch. — [App Store listing](https://apps.apple.com/us/app/mint-budget-planner-tracker/id300238550)
- Apple Watch: it reviews day-to-day spending and suggests a spending goal. It lets you create and manage a discretionary spending budget, with a quick glance showing how much you can spend today and your progress on the monthly goal. — [Wise Bread](https://www.wisebread.com/8-apple-watch-apps-thatll-save-you-money); [AppleVis listing](https://www.applevis.com/apps/watch/finance/mintpersonal-finance-money)
- Alerts: subscription price-change alerts and bill-increase alerts (2021). — [Intuit press release](https://investors.intuit.com/news-events/press-releases/detail/196/all-new-mint-ios-app-launched-to-empower-financial-freedom)
- Customised reminders so you do not miss payments. — [Listen Money Matters](https://www.listenmoneymatters.com/mint-review/)
- A "net worth widget" is mentioned only in a search summary, not traced to a specific Mint source. — (unverified; see Gaps)

### Gaps
- iOS 14+ home-screen widget: not confirmed by a primary source.
- Face ID support date: no source found.

## Reviewer praise and criticism (including ad and offer clutter); end of life

### Takeaway
Reviewers praised the single-screen overview of accounts, bills and budgets, the automatic categorisation, free credit score and colourful charts. They consistently criticised ads and partner offers (credit cards, loans, TurboTax), occasional sync glitches and no shared or joint accounts. Mint shut down on 23 March 2024, and users were moved to Credit Karma.

### Cited Findings
- Praise: "The app shows accounts, bills, and budgets on a single screen." — [PCWorld review](https://www.pcworld.com/article/407663/mint-review-financial-budgeting-app.html)
- Praise: automatic categorisation with colourful graphics, custom reminders and free credit score. Criticism: ads, no joint accounts and "some glitchy integrations". — [Listen Money Matters](https://www.listenmoneymatters.com/mint-review/)
- Criticism: the app is "cluttered with partner offers". Intuit "serves up a barrage of financial offers", from TurboTax to credit cards and loans, and reviewers found the ads excessive. — [Tom's Guide 2022](https://www.tomsguide.com/reviews/intuit-mint-2022); [SmartAsset](https://smartasset.com/financial-advisor/mint-review)
- Criticism (c. 2014): the Overview "Advice" button "tends to be sales pitches for various credit cards and savings accounts". — [AFB AccessWorld](https://afb.org/aw/15/4/15583)
- A search summary says there was no paid ad-free option, with later limited ad-removal. This is unverified, and I am not confident about the "later" part. — [search summary; see SmartAsset](https://smartasset.com/financial-advisor/mint-review)
- Shutdown: announced November 2023, first for January 2024, then moved to **March 23, 2024**. Migration to Credit Karma carried accounts and net worth but not Mint-style budgeting. — [Rocket Money](https://www.rocketmoney.com/learn/personal-finance/mint-app-shutting-down); [Spendify](https://spendify.money/blog/mint-shut-down-now-what/); [Miles to Memories](https://milestomemories.com/mint-will-shut-down-next-year/)
- Mint once had more than 20 million users (the 2021 press release says 10M+ active annual users). — [Spendify](https://spendify.money/blog/mint-shut-down-now-what/); [Intuit press release](https://investors.intuit.com/news-events/press-releases/detail/315/mint-reinvents-app-experience-paving-the-way-to-a-better-money-management-solution)

### Inferences
- Offers were built into the core IA throughout: the "Advice" button around 2014, the "Ways to Save" web tab, and partner offers inside MintSights cards from 2021. A Mint-like app that wants to avoid the main complaint should keep offers out of the primary feed.

### Gaps
- I found no reachable Reddit threads with dated UX complaints about the 2021 redesign. Specific criticism of the redesign (for example lost features or extra taps) is not documented here.
