// The golden questions for "Ask this document", shared by scripts/ask-eval.ts.
//
// `expect` is a passage substring that answers the question, or null when the document does not
// answer it (the words are in the document, the fact is not).
//
// Each answerable question belongs to one set, so retrieval can be tuned without fooling itself:
//   tune   the original questions, minus the held-out third
//   hold   every third of the original questions of each document (the 3rd, 6th, 9th, ... in file
//          order, counted per document): chosen by position before any tuning, never by outcome
//   new    questions written afterwards from the documents' own text, on these documents and others
//   confirm  a last batch written after the levers to try were fixed, to confirm or refute them
// A change to retrieval is kept only when it helps `tune` and does not hurt `hold`, `new` and `confirm`.

export type Golden = {
  slug: string;
  question: string;
  expect: string | null;
};

export type GoldenSet = "tune" | "hold" | "new" | "confirm";

export type TaggedGolden = Golden & { set: GoldenSet };

const ORIGINAL: Golden[] = [

  // resnet: prose, an equation and tables
  { slug: "resnet", question: "How many layers is the deepest network evaluated on ImageNet?", expect: "152 layers" },
  { slug: "resnet", question: "What top-5 error did the ensemble achieve on the ImageNet test set?", expect: "3.57% error" },
  { slug: "resnet", question: "Which competition did the residual nets win first place in?", expect: "1st place in the ILSVRC 2015 classification" },
  { slug: "resnet", question: "What is the degradation problem?", expect: "a degradation problem has been exposed" },
  { slug: "resnet", question: "What is the formula of a residual building block?", expect: "y = F(x, {Wi}) + x" },
  { slug: "resnet", question: "How many layers does the over 1000 layer network on CIFAR-10 have?", expect: "1202-layer network" },
  { slug: "resnet", question: "What weight decay and momentum are used for training on ImageNet?", expect: "weight decay of 0.0001 and a momentum of 0.9" },
  { slug: "resnet", question: "What mini-batch size does SGD use?", expect: "mini-batch size of 256" },
  { slug: "resnet", question: "What learning rate is used to warm up the 110-layer ResNet?", expect: "we use 0.01 to warm up the training" },
  { slug: "resnet", question: "What is VLAD?", expect: "VLAD [18] is a representation that encodes by the residual vectors" },
  { slug: "resnet", question: "What is the validation error of the 34-layer plain network and the 34-layer ResNet in Table 2?", expect: "34 layers | 28.54 | 25.03" },
  { slug: "resnet", question: "What are the top-5 localization errors of OverFeat on val and test?", expect: "OverFeat [40] (ILSVRC’13) | 30.0 | 29.9" },
  // fw9: a short form with instructions
  { slug: "fw9", question: "What percent is backup withholding?", expect: "24% of such payments" },
  { slug: "fw9", question: "What is the penalty for failing to furnish your correct TIN to a requester?", expect: "a penalty of $50 for each such failure" },
  { slug: "fw9", question: "How many days do I have to get a TIN for readily tradable instruments?", expect: "60 days to get a TIN" },
  { slug: "fw9", question: "Which kinds of numbers can be a taxpayer identification number?", expect: "individual taxpayer identification number (ITIN)" },
  { slug: "fw9", question: "Who do I give the form to?", expect: "Give form to the requester. Do not send to the IRS." },
  { slug: "fw9", question: "What code do I enter for the tax classification of an LLC?", expect: "enter the appropriate code (C, S, or P)" },
  { slug: "fw9", question: "What number is an individual's TIN generally?", expect: "generally your social security number (SSN)" },
  { slug: "fw9", question: "What do I enter on line 1 if I am a sole proprietor?", expect: "enter the owner’s name on line 1" },
  // p17: IRS Publication 17, a large document of mostly short list items
  { slug: "p17", question: "What is the standard deduction for head of household in 2025?", expect: "Head of household—$23,625" },
  { slug: "p17", question: "What is the standard deduction for a single filer in 2025?", expect: "Single or Married filing separately—$15,750" },
  { slug: "p17", question: "What is the standard deduction for married filing jointly in 2025?", expect: "Married filing jointly or Qualifying surviving spouse—$31,500" },
  { slug: "p17", question: "When is the due date to file Form 1040 for 2025?", expect: "File Form 1040 or 1040-SR by April 15, 2026" },
  { slug: "p17", question: "How much can I deduct for qualified tips?", expect: "can’t deduct more than $25,000 of those tips" },
  { slug: "p17", question: "How much qualified overtime compensation can I deduct?", expect: "deduct up to $12,500" },
  { slug: "p17", question: "How much car loan interest can I deduct?", expect: "deduct up to $10,000" },
  { slug: "p17", question: "What is the 401(k) contribution limit for 2025?", expect: "$23,500;" },
  { slug: "p17", question: "What is the limit on the state and local tax deduction?", expect: "increased to $40,000" },
  { slug: "p17", question: "What is a Trump account?", expect: "called a Trump account" },
  // frcp-2025: the Federal Rules of Civil Procedure, long numbered rules
  { slug: "frcp-2025", question: "How many days does a defendant have to serve an answer after being served with the summons and complaint?", expect: "within 21 days after being served with the summons and complaint" },
  { slug: "frcp-2025", question: "How long does a defendant who waives service have to serve an answer?", expect: "60 days after the request was sent" },
  { slug: "frcp-2025", question: "What must a summons contain?", expect: "A summons must" },
  { slug: "frcp-2025", question: "When is a motion to dismiss treated as one for summary judgment?", expect: "must be treated as one for summary judgment under Rule 56" },
  { slug: "frcp-2025", question: "How long do I have to demand a jury trial on other issues after being served with a demand?", expect: "within 14 days after being served with the demand" },
  { slug: "frcp-2025", question: "How is the last day of a time period counted when it is a weekend or holiday?", expect: "the period continues to run until the end of the next day" },
  { slug: "frcp-2025", question: "Can a party amend its pleading once as a matter of course?", expect: "A party may amend its pleading once as a matter of course" },
  { slug: "frcp-2025", question: "When must initial disclosures be made after the Rule 26(f) conference?", expect: "within 14 days after the parties’ Rule 26(f) conference" },
  { slug: "frcp-2025", question: "On what grounds may a court set aside an entry of default?", expect: "set aside an entry of default for good cause" },
  { slug: "frcp-2025", question: "Which discovery materials must not be filed until they are used in the proceeding?", expect: "depositions, interrogatories, requests for documents" },
  // Unanswerable: the question's words occur in the document, the fact does not. Retrieval mode skips
  // these; --model mode counts a validated answer to one as "invented".
  { slug: "resnet", question: "What batch size is used for training on CIFAR-100?", expect: null },
  { slug: "resnet", question: "Who is the chief executive of Microsoft Research?", expect: null },
  { slug: "fw9", question: "What is the fee for filing Form W-9 electronically?", expect: null },
  { slug: "p17", question: "What was the standard deduction for a single filer in 2019?", expect: null },
  { slug: "frcp-2025", question: "How many jurors must sit on a criminal jury in state court?", expect: null },
];

const NEW: Golden[] = [

  // ---- new questions (set "new"), written from the documents' own text, not from what the ranker
  // returns. The tuning of retrieval never saw them.
  // resnet
  { slug: "resnet", question: "How many FLOPs does the 152-layer ResNet have?", expect: "11.3 billion FLOPs" },
  { slug: "resnet", question: "What single-model top-5 validation error does the 152-layer ResNet reach?", expect: "single-model top-5 validation error of 4.49%" },
  { slug: "resnet", question: "What mini-batch size is used for training on CIFAR-10?", expect: "minibatch size of 128" },
  { slug: "resnet", question: "How many training images does CIFAR-10 have?", expect: "50k training images and 10k testing images" },
  { slug: "resnet", question: "What center-crop error does VGG report with ground truth classes for localization?", expect: "center-crop error of 33.1%" },
  { slug: "resnet", question: "What mAP does the detection system reach on PASCAL VOC 2007?", expect: "85.6% mAP on PASCAL VOC 2007" },
  { slug: "resnet", question: "How many parameters does the 1202-layer network have?", expect: "unnecessarily large (19.4M)" },
  { slug: "resnet", question: "Why are identity shortcuts important for the bottleneck design?", expect: "time complexity and model size are doubled" },
  // fw9
  { slug: "fw9", question: "What is the penalty for a false statement with no reasonable basis that results in no backup withholding?", expect: "you are subject to a $500 penalty" },
  { slug: "fw9", question: "Where do I forward an unsolicited email claiming to be from the IRS?", expect: "forward this message to phishing@irs.gov" },
  { slug: "fw9", question: "What do I put in the TIN space if I do not have a TIN yet?", expect: "enter “Applied For” in the space for the TIN" },
  { slug: "fw9", question: "Who signs the certification on a joint account?", expect: "only the person whose TIN is shown in Part I should sign" },
  { slug: "fw9", question: "Are corporations exempt from backup withholding on attorneys' fees?", expect: "Corporations are not exempt from backup withholding with respect to attorneys’ fees" },
  { slug: "fw9", question: "Which exempt payee code is for the United States or its agencies?", expect: "2—The United States or any of its agencies or instrumentalities" },
  // p17
  { slug: "p17", question: "What is the 2025 standard mileage rate for business use of a vehicle?", expect: "70 cents a mile" },
  { slug: "p17", question: "What is the 2025 mileage rate for volunteer driving for charities?", expect: "14 cents a mile" },
  { slug: "p17", question: "What is the capital loss deduction limit for married filing separately?", expect: "Your capital loss deduction limit is $1,500 (instead of $3,000 on a joint return)" },
  { slug: "p17", question: "What is the maximum child tax credit for each qualifying child?", expect: "amount of CTC for each qualifying child increased to $2,200" },
  { slug: "p17", question: "Who is eligible to use Free File?", expect: "you are eligible for Free File, a free tax software service offered by IRS partners" },
  { slug: "p17", question: "At what income do social security benefits become taxable for a single person?", expect: "$25,000 ($32,000 if married filing jointly)" },
  // frcp-2025
  { slug: "frcp-2025", question: "How long does a party have to answer interrogatories?", expect: "within 30 days after being served with the interrogatories" },
  { slug: "frcp-2025", question: "When is a matter admitted after a request for admission?", expect: "A matter is admitted unless, within 30 days after being served" },
  { slug: "frcp-2025", question: "How long do I have to respond in writing to a request for production?", expect: "must respond in writing within 30 days after being served" },
  { slug: "frcp-2025", question: "What is the deadline to respond to an amended pleading?", expect: "within the time remaining to respond to the original pleading or within 14 days after service of the amended pleading" },
  { slug: "frcp-2025", question: "How many interrogatories may one party serve on another?", expect: "no more than 25 written interrogatories" },
  { slug: "frcp-2025", question: "How long can a deposition last?", expect: "a deposition is limited to one day of 7 hours" },
  { slug: "frcp-2025", question: "How long does a party have to answer a counterclaim?", expect: "A party must serve an answer to a counterclaim or crossclaim within 21 days after being served" },
  // other documents
  { slug: "scotus-mahmoud-v-taylor", question: "Who delivered the opinion of the Court?", expect: "Justice Alito delivered the opinion of the Court" },
  { slug: "scotus-mahmoud-v-taylor", question: "What must the parents show to get a preliminary injunction?", expect: "the parents must show that: they are likely to succeed on the merits" },
  { slug: "scotus-mahmoud-v-taylor", question: "Does a plaintiff have to wait for damage to occur before filing suit when First Amendment rights are at stake?", expect: "need not wait for the damage to occur" },
  { slug: "scotus-mahmoud-v-taylor", question: "What should the Board be ordered to do until appellate review is finished?", expect: "the Board should be ordered to notify the petitioners in advance" },
  { slug: "irs-w4-2026", question: "What phone number reaches the Social Security Administration?", expect: "contact SSA at 800-772-1213" },
  { slug: "irs-w4-2026", question: "How much do I multiply qualifying children under 17 by in Step 4?", expect: "Multiply the number of qualifying children under age 17 by $2,200" },
  { slug: "irs-w4-2026", question: "Who can claim exemption from withholding for 2026?", expect: "you had no federal income tax liability in 2025" },
  { slug: "irs-w4-2026", question: "What total income allows the child credit amounts in Step 4 for a married couple filing jointly?", expect: "$200,000 or less ($400,000 or less if married filing jointly)" },
  { slug: "uscis-i9", question: "By when must an employee complete Section 1?", expect: "no later than the first day of employment" },
  { slug: "cdc-firstline-slides-es", question: "¿Qué es el SARS-CoV-2?", expect: "SARS-CoV-2 es el nombre científico oficial del virus, el microbio que causa el COVID-19" },
  { slug: "cdc-firstline-slides-es", question: "¿Cuántas partes principales tiene un virus?", expect: "las tres (3) partes principales de un virus" },
  { slug: "cdc-antibiotics-trifold", question: "How many people die each year from antibiotic-resistant infections?", expect: "At least 23,000 people die as a result" },
  { slug: "cdc-antibiotics-trifold", question: "What does antibiotic resistance mean?", expect: "Antibiotic resistance does not mean the body is becoming resistant to antibiotics" },
  { slug: "census-p60-273", question: "What was the official poverty rate in 2020?", expect: "The official poverty rate in 2020 was 11.4 percent" },
  { slug: "census-p60-273", question: "When was the last significant decline in median household income before 2020?", expect: "first statistically significant decline in median household income since 2011" },
  { slug: "nasa-se-handbook", question: "What is verification?", expect: "Verification is a formal process, using the method of test, analysis, inspection or" },
  { slug: "nasa-se-handbook", question: "What is product validation?", expect: "Confirming that a verified end product satisfies the stakeholder expectations for its intended use" },
  { slug: "nasa-se-handbook", question: "What does the critical design review demonstrate?", expect: "maturity of the design is appropriate to support proceeding with full" },
  { slug: "epa-ghg-inventory-2024", question: "Which economic sector produced the largest share of US greenhouse gas emissions in 2022?", expect: "emissions from transportation activities accounted for the largest portion (28.4 percent)" },
  { slug: "epa-ghg-inventory-2024", question: "How much of total emissions did carbon sequestration from land use and forestry offset in 2022?", expect: "offset 14.5 percent of total emissions in 2022" },
];

const CONFIRM: Golden[] = [
  { slug: "resnet", question: "How many FLOPs does the 34-layer baseline have?", expect: "baseline has 3.6 billion FLOPs" },
  { slug: "resnet", question: "What percent of VGG-19's FLOPs is the 34-layer baseline?", expect: "which is only 18% of VGG-19" },
  { slug: "resnet", question: "What percent relative improvement did they get on COCO object detection?", expect: "28% relative improvement on the COCO object detection dataset" },
  { slug: "resnet", question: "How many object categories does the ImageNet detection task involve?", expect: "involves 200 object categories" },
  { slug: "resnet", question: "How many object categories does MS COCO have?", expect: "involves 80 object categories" },
  { slug: "resnet", question: "How many training images is the ImageNet classification model trained on?", expect: "trained on the 1.28 million training images" },
  { slug: "fw9", question: "What does the IRS not request through email?", expect: "does not request personal detailed information through email" },
  { slug: "fw9", question: "Which form do I use to apply for a social security number?", expect: "get Form SS-5, Application for a Social Security Card" },
  { slug: "fw9", question: "Which form reports canceled debt?", expect: "Form 1099-C (canceled debt)" },
  { slug: "fw9", question: "What is the FATCA exemption code for a real estate investment trust?", expect: "G—A real estate investment trust" },
  { slug: "fw9", question: "What happens if I do not return Form W-9 with a TIN?", expect: "you might be subject to backup withholding" },
  { slug: "p17", question: "What is the maximum additional child tax credit for each qualifying child?", expect: "The maximum amount of ACTC for each qualifying child is $1,700" },
  { slug: "scotus-mahmoud-v-taylor", question: "Which justices joined the opinion of the Court?", expect: "in which Roberts, C. J., and Thomas, Gorsuch, Kavanaugh, and Barrett, JJ., joined" },
  { slug: "scotus-mahmoud-v-taylor", question: "In which school year did the Board introduce the storybooks?", expect: "During the 2022–2023 school year" },
  { slug: "irs-w4-2026", question: "Where do I find the Multiple Jobs Worksheet?", expect: "Use the Multiple Jobs Worksheet on page 3" },
  { slug: "irs-w4-2026", question: "What should I do in Step 2 if I have self-employment income?", expect: "If you or your spouse have self-employment income, use this option" },
  { slug: "census-p60-273", question: "What was the median household income in 2019?", expect: "the 2019 median of $69,560" },
  { slug: "cdc-antibiotics-trifold", question: "Which infection can diarrhea signal while taking antibiotics?", expect: "diarrhea, since that could be Clostridium difficile infection" },
  { slug: "uscis-i9", question: "Which supplement is used for reverification or rehire?", expect: "complete Supplement B, Reverification and Rehire on Page 4" },
  { slug: "cdc-firstline-slides-es", question: "¿Qué medidas de control de infecciones se centran en mantener fuera del aire?", expect: "mantener las gotitas respiratorias fuera del aire" },
];

const seen = new Map<string, number>();

export const GOLDEN: TaggedGolden[] = [
  ...ORIGINAL.map((g): TaggedGolden => {
    if (g.expect === null) return { ...g, set: "tune" };
    const i = seen.get(g.slug) ?? 0;
    seen.set(g.slug, i + 1);
    return { ...g, set: i % 3 === 2 ? "hold" : "tune" };
  }),
  ...NEW.map((g): TaggedGolden => ({ ...g, set: "new" })),
  ...CONFIRM.map((g): TaggedGolden => ({ ...g, set: "confirm" })),
];

/** Documents the golden questions read: `bun run docs:build <these>` before running the eval. */
export const GOLDEN_SLUGS = [...new Set(GOLDEN.map((g) => g.slug))];
