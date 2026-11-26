// Sample documents for the test page. `source` is relative to the repo's samples/.
// scripts/build-docs.ts converts each into public/docs/<slug>/ and copies the PDF to
// public/pdfs/<slug>.pdf.

/** The reflow.run.Options keywords a document converts with, over docs:build's own (engine and
 *  alt text from the env). They are part of its provenance: changing them makes it stale. */
export type ConvertOptions = {
  engine?: string;
  ocr?: string;
  alt_text?: string;
  alt_text_model?: string;
  assist_labels?: boolean;
  assist_structure?: boolean;
  assist_scan_fields?: boolean;
  assist_page_vision?: boolean;
  assist_provider?: string;
  assist_model?: string;
  /** With engine "direct": the model that transcribes each page (docs/direct.md). */
  direct_model?: string;
  /** With engine "direct": the prompt variant (base, complete, forms, example). */
  direct_prompt?: string;
  /** After the heuristics, a Claude model proposes structural edits (docs/polish.md). */
  polish?: boolean;
  polish_model?: string;
};

/** A sample: one document of the site, whatever its variants (src/pipelines.ts). `options` are its
 *  own convert options, under every pipeline's. */
export type SampleDoc = {
  slug: string; source: string; title: string; description: string; options?: ConvertOptions;
  /** The home page section the document is listed in, from its source folder (collectionOf). */
  collection: Collection;
};

/** The home page's sections, in the order it lists them. */
export const COLLECTIONS = [
  { id: "azblue", name: "AZBlue" },
  { id: "docaccess", name: "DocAccess" },
  { id: "other", name: "Other" },
] as const;
export type Collection = (typeof COLLECTIONS)[number]["id"];

/** The collection of a `source` path: its top folder under samples/ when that names one, else "other". */
export function collectionOf(source: string): Collection {
  const folder = source.split("/")[0];
  return folder === "azblue" || folder === "docaccess" ? folder : "other";
}

const ENTRIES: Omit<SampleDoc, "collection">[] = [
  { slug: "resnet", source: "resnet.pdf", title: "Deep Residual Learning for Image Recognition",
    description: "Two-column research paper with charts, diagrams and tables" },
  { slug: "fw9", source: "fw9.pdf", title: "Form W-9: Request for Taxpayer Identification Number",
    description: "Tagged government form" },
  { slug: "p17", source: "p17.pdf", title: "IRS Publication 17: Your Federal Income Tax",
    description: "142-page, three-column publication with tables" },
  { slug: "naca", source: "naca-ocr.pdf", title: "NACA Technical Report (scanned)",
    description: "Scanned report with an existing OCR text layer" },
  { slug: "pima-directory", source: "azblue/ma-provider-directory-pima.pdf",
    title: "Provider/Pharmacy Directory: Pima County",
    description: "684-page provider directory" },
  { slug: "maricopa-directory", source: "azblue/ma-provider-directory-maricopa-pinal.pdf",
    title: "Provider/Pharmacy Directory: Maricopa & Pinal Counties",
    description: "2,616-page provider directory" },
  { slug: "budget-appendix", source: "budget-app.pdf", title: "Budget of the U.S. Government FY2025: Appendix",
    description: "1,298 pages of budget tables" },
  { slug: "nchs-bar-chart", source: "corpus/nchs-bar-chart.pdf", title: "QuickStats from NCHS: Celebrating 15 Years",
    description: "Two-page CDC report with a vector pie chart and a vector bar chart" },
  { slug: "census-p60-273", source: "corpus/census-p60-273.pdf", title: "Income and Poverty in the United States: 2020",
    description: "92-page Census report with bar, dot and line charts and rotated tables" },
  { slug: "cdc-firstline-slides-es", source: "corpus/cdc-firstline-slides-es.pdf", title: "Project Firstline: Slides (Spanish)",
    description: "14-slide deck exported to PDF with photo title slides and Spanish bullets" },
  { slug: "uscis-i9", source: "corpus/uscis-i9.pdf", title: "Form I-9: Employment Eligibility Verification",
    description: "Fillable form with 130 fields and checkboxes" },
  { slug: "irs-w4-2026", source: "corpus/irs-w4-2026.pdf", title: "2026 Form W-4: Employee's Withholding Certificate",
    description: "XFA-enabled AcroForm with text fields, checkboxes and a worksheet" },
  { slug: "cdc-hepb-arabic", source: "corpus/cdc-hepb-arabic.pdf", title: "Hepatitis B: Are You At Risk? (Arabic and English)",
    description: "Right-to-left Arabic text layer on the front, English on the back" },
  { slug: "scotus-mahmoud-v-taylor", source: "corpus/scotus-mahmoud-v-taylor.pdf", title: "Mahmoud v. Taylor (Supreme Court slip opinion)",
    description: "136-page opinion with footnotes, a syllabus and dissents; owner-password encrypted" },
  { slug: "frcp-2025", source: "corpus/frcp-2025.pdf", title: "Federal Rules of Civil Procedure (December 1, 2025)",
    description: "142-page rules with nested enumerations; not tagged" },
  { slug: "nasa-se-handbook", source: "corpus/nasa-se-handbook.pdf", title: "NASA Systems Engineering Handbook (Rev 2)",
    description: "356-page handbook with callout boxes and rotated tables" },
  { slug: "epa-ghg-inventory-2024", source: "corpus/epa-ghg-inventory-2024.pdf",
    title: "Inventory of U.S. Greenhouse Gas Emissions and Sinks: 1990-2022",
    description: "919-page main text with charts, long tables and footnotes" },
  { slug: "cdc-antibiotics-trifold", source: "corpus/cdc-antibiotics-trifold.pdf", title: "Antibiotics Aren't Always the Answer (tri-fold)",
    description: "Two-sided landscape tri-fold brochure with six panels" },
  { slug: "cdc-antibiotics-korean", source: "corpus/cdc-antibiotics-korean.pdf", title: "Antibiotics Aren't Always the Answer (Korean)",
    description: "Two-page Korean fact sheet with Hangul text and a check-mark table" },
  { slug: "az-accident-insurance-flyer", source: "azblue/site/accident-insurance-flyer.pdf", title: "Accident insurance flyer (employers)",
    description: "azblue.com: 2-page flyer" },
  { slug: "az-deceased-member-affidavit", source: "azblue/site/deceased-member-affidavit.pdf", title: "Deceased member affidavit form",
    description: "azblue.com: 2-page form (affidavit)" },
  { slug: "az-eft-enrollment", source: "azblue/site/eft-enrollment.pdf", title: "ERA/EFT enrollment form (providers)",
    description: "azblue.com: 2-page form" },
  { slug: "az-group-contract-form", source: "azblue/site/group-contract-form.pdf", title: "Group contract form (providers)",
    description: "azblue.com: 3-page form" },
  { slug: "az-group-ppo-sob", source: "azblue/site/group-ppo-sob.pdf", title: "Group BluePreferred PPO 2000/50 SBC/summary of benefits",
    description: "azblue.com: 10-page summary of benefits (tables)" },
  { slug: "az-hca-member-handbook-2026", source: "azblue/site/hca-member-handbook-2026.pdf", title: "2026 HCA (Medicaid) member handbook",
    description: "azblue.com: 259-page member handbook" },
  { slug: "az-hca-nondiscrim-multilang", source: "azblue/site/hca-nondiscrim-multilang.pdf", title: "HCA non-discrimination notice, multi-language insert",
    description: "azblue.com: 4-page notice (multilingual taglines), multiple" },
  { slug: "az-hipaa-npp", source: "azblue/site/hipaa-npp.pdf", title: "HIPAA notice of privacy practices",
    description: "azblue.com: 6-page notice" },
  { slug: "az-ma-appeals-form-2026", source: "azblue/site/ma-appeals-form-2026.pdf", title: "2026 Medicare Advantage appeals and grievance form (fillable)",
    description: "azblue.com: 4-page form (fillable)" },
  { slug: "az-ma-eoc-classic-pima-es", source: "azblue/site/ma-eoc-classic-pima-es.pdf", title: "2026 MA evidence of coverage, Classic, Pima (Spanish)",
    description: "azblue.com: 244-page evidence of coverage, Spanish" },
  { slug: "az-ma-formulary-2026-en", source: "azblue/site/ma-formulary-2026-en.pdf", title: "2026 MA formulary",
    description: "azblue.com: 105-page formulary (drug list tables)" },
  { slug: "az-ma-otc-rewards-flyer", source: "azblue/site/ma-otc-rewards-flyer.pdf", title: "2026 MA OTC rewards benefit flyer",
    description: "azblue.com: 2-page flyer, English/Spanish (per filename)" },
  { slug: "az-ma-sob-classic-maricopa-en", source: "azblue/site/ma-sob-classic-maricopa-en.pdf", title: "2026 MA summary of benefits, Classic, Maricopa/Pinal",
    description: "azblue.com: 36-page summary of benefits (tables)" },
  { slug: "az-medical-claim-es", source: "azblue/site/medical-claim-es.pdf", title: "Medical claim form (Spanish)",
    description: "azblue.com: 2-page form, Spanish" },
  { slug: "az-ms-restrict-records-form", source: "azblue/site/ms-restrict-records-form.pdf", title: "Medicare Supplement request to restrict health records (fillable)",
    description: "azblue.com: 1-page form (fillable)" },
  { slug: "az-nurse-on-call-flyer", source: "azblue/site/nurse-on-call-flyer.pdf", title: "Nurse on call flyer (employers)",
    description: "azblue.com: 1-page flyer" },
  { slug: "az-privacy-release-spanish", source: "azblue/site/privacy-release-spanish.pdf", title: "Confidential information release (Spanish)",
    description: "azblue.com: 2-page form, Spanish" },
  { slug: "az-tier-1a-rx-list", source: "azblue/site/tier-1a-rx-list.pdf", title: "Tier 1A prescription drug list",
    description: "azblue.com: 2-page formulary (drug list)" },
  { slug: "az-tucson-maternity-brochure", source: "azblue/site/tucson-maternity-brochure.pdf", title: "City of Tucson maternity care brochure (image-heavy)",
    description: "azblue.com: 4-page brochure (images)" },
  { slug: "az-why-choose-blue-es", source: "azblue/site/why-choose-blue-es.pdf", title: "2025 'why choose Blue' flyer (Spanish)",
    description: "azblue.com: 2-page flyer, Spanish" },
  { slug: "da-chem-math", source: "docaccess/01-chem-math.pdf", title: "Math and Chemistry Homework (CHEM 162)",
    description: "DocAccess demo document, 1 page" },
  { slug: "da-building-permit-applications", source: "docaccess/02-building-permit-applications.pdf", title: "Building Permit Applications 1987-1988",
    description: "DocAccess demo document, 8 pages" },
  { slug: "da-assessment-appeal-application", source: "docaccess/03-assessment-appeal-application.pdf", title: "Form - Assessment Appeal Application",
    description: "DocAccess demo document, 4 pages" },
  { slug: "da-school-district-calendar", source: "docaccess/04-school-district-calendar.pdf", title: "Color-Coded school district calendar",
    description: "DocAccess demo document, 1 page" },
  { slug: "da-legislation-tracked-changes", source: "docaccess/05-legislation-tracked-changes.pdf", title: "Legislation (tracked changes)",
    description: "DocAccess demo document, 54 pages" },
  { slug: "da-complex-rfp-diagrams", source: "docaccess/06-complex-rfp-diagrams.pdf", title: "Request for Proposal - Computer Aided Dispatch (Kitsap 911)",
    description: "DocAccess demo document, 12 pages" },
  { slug: "da-financial-audit-comprehensive", source: "docaccess/07-financial-audit-comprehensive.pdf", title: "Comprehensive financial audit",
    description: "DocAccess demo document, 93 pages" },
  { slug: "da-multi-column-brochure", source: "docaccess/08-multi-column-brochure.pdf", title: "Multi-column brochure and scanned map",
    description: "DocAccess demo document, 3 pages" },
  { slug: "da-scanned-budget-handwriting", source: "docaccess/09-scanned-budget-handwriting.pdf", title: "Scanned budget with handwriting",
    description: "DocAccess demo document, 12 pages" },
  { slug: "da-directory-providers", source: "docaccess/10-directory-providers.pdf", title: "Directory of providers",
    description: "DocAccess demo document, 20 pages" },
  { slug: "da-timelines-difficult-graphics", source: "docaccess/11-timelines-difficult-graphics.pdf", title: "Carmel Area Wastewater District Final Budget 2024-25",
    description: "DocAccess demo document, 12 pages" },
  { slug: "az-advance-directive-notice-bilingual", source: "azblue/site/advance-directive-notice-bilingual.pdf", title: "Medicare Advantage Notice of Advance Directives",
    description: "azblue.com: 4-page PDF" },
  { slug: "az-appt-representative-bilingual", source: "azblue/site/appt-representative-bilingual.pdf", title: "Medicare Advantage Appointment of Representative Instructions",
    description: "azblue.com: 4-page PDF" },
  { slug: "az-bluecard-provider-guide", source: "azblue/site/bluecard-provider-guide.pdf", title: "The BlueCard Program Provider Guide",
    description: "azblue.com: 37-page PDF" },
  { slug: "az-bluedental-provider-manual", source: "azblue/site/bluedental-provider-manual.pdf", title: "2026 BlueDental Provider Manual",
    description: "azblue.com: 21-page PDF" },
  { slug: "az-code-of-conduct", source: "azblue/site/code-of-conduct.pdf", title: "Code Blue: Blue Cross Blue Shield of Arizona’s Code of Conduct 2026-2027",
    description: "azblue.com: 39-page PDF" },
  { slug: "az-employer-product-guide", source: "azblue/site/employer-product-guide.pdf", title: "2026 Employer Health Plan Product Guide",
    description: "azblue.com: 28-page PDF" },
  { slug: "az-hos-fall-risk-tip-sheet", source: "azblue/site/hos-fall-risk-tip-sheet.pdf", title: "2026 Fall Prevention Tip Sheet",
    description: "azblue.com: 2-page PDF" },
  { slug: "bluejourney-directory", source: "azblue/ma-provider-directory-bluejourney-sp.pdf", title: "2024 Provider/Pharmacy Directory",
    description: "azblue.com: 2,640-page PDF" },
  { slug: "az-ma-vision-flyer-bilingual", source: "azblue/site/ma-vision-flyer-bilingual.pdf", title: "Protect your quality of life by protecting your eyes.",
    description: "azblue.com: 2-page PDF" },
  { slug: "az-medsup-outline-coverage", source: "azblue/site/medsup-outline-coverage.pdf", title: "2026 Outline of Coverage",
    description: "azblue.com: 28-page PDF" },
  { slug: "az-medsup-rate-sheet", source: "azblue/site/medsup-rate-sheet.pdf", title: "2026 Medicare Supplement Plan Monthly Rates at a Glance",
    description: "azblue.com: 4-page PDF" },
  { slug: "az-newborn-reporting-form", source: "azblue/site/newborn-reporting-form.pdf", title: "Newborn Notification",
    description: "azblue.com: 1-page PDF" },
  { slug: "az-pharmacy-guideline-p007", source: "azblue/site/pharmacy-guideline-p007.pdf", title: "Pharmacy Coverage Guideline: Biologic and Immunological Agents – Injection",
    description: "azblue.com: 32-page PDF" },
  { slug: "az-provider-portal-guide", source: "azblue/site/provider-portal-guide.pdf", title: "Provider Portal User Guide",
    description: "azblue.com: 29-page PDF" },
  { slug: "az-psych-pa-form", source: "azblue/site/psych-pa-form.pdf", title: "Prior Authorization and Continued Stay Request Form for Mental Health and Subacute Facilities",
    description: "azblue.com: 1-page PDF" },
  { slug: "az-sbc-2026-p7950", source: "azblue/site/sbc-2026-p7950.pdf", title: "Summary of Benefits and Coverage: What this Plan Covers & What You Pay for Covered Services — EverydayHealth PPO Gold 2500",
    description: "azblue.com: 10-page PDF" },
  { slug: "az-tucson-employee-booklet", source: "azblue/site/tucson-employee-booklet.pdf", title: "Open Enrollment 2026",
    description: "azblue.com: 44-page PDF" },
  { slug: "az-tucson-member-guide", source: "azblue/site/tucson-member-guide.pdf", title: "Welcome City of Tucson Employees and Retirees 2026 – 2027",
    description: "azblue.com: 24-page PDF" },
  { slug: "az-tucson-sharecare-flyer", source: "azblue/site/tucson-sharecare-flyer.pdf", title: "What’s Your RealAge?",
    description: "azblue.com: 2-page PDF" },
  { slug: "az-claim-submission-guide", source: "azblue/site/19-claim-submission-provider-operating-guide.pdf", title: "2026 AZ Blue Provider Operating Guide",
    description: "azblue.com: 46-page PDF" },
  { slug: "az-cptii-codes-tip-sheet", source: "azblue/site/2286089-26-cptii-codes_tip-sheet_final.pdf", title: "2026 CPT Category II Codes for HEDIS Reporting",
    description: "azblue.com: 3-page PDF" },
  { slug: "az-assistant-surgeon-codes-2026-01", source: "azblue/site/assistant-surgeon-codes-2026-01.pdf", title: "CPT/HCPCS Codes that are Eligible for an Assistant Surgeon",
    description: "azblue.com: 9-page PDF" },
  { slug: "az-audit-compliance-and-risk-committee-charter", source: "azblue/site/audit-compliance-and-risk-committee-charter.pdf", title: "Audit, Compliance, and Risk Committee Charter",
    description: "azblue.com: 4-page PDF" },
  { slug: "az-dental-networks-quick-ref", source: "azblue/site/az-blue-dental-products-and-networks-quick-reference-guide.pdf", title: "Dental Products and Networks – Quick Guide",
    description: "azblue.com: 1-page PDF" },
  { slug: "az-benefits-and-talent", source: "azblue/site/benefits-and-talent.pdf", title: "The benefits and talent report",
    description: "azblue.com: 3-page PDF" },
  { slug: "az-bluehpn-benefits", source: "azblue/site/bluehpn-benefits.pdf", title: "Introducing a new option: Blue High Performance Network (BlueHPN)",
    description: "azblue.com: 2-page PDF" },
  { slug: "az-checkup-checklist", source: "azblue/site/checkup-checklist.pdf", title: "The Ultimate Guy’s Checkup Checklist and The Ultimate Checkup Checklist",
    description: "azblue.com: 2-page PDF" },
  { slug: "az-claim-form-instruction-booklet", source: "azblue/site/claim-form-instruction-booklet.pdf", title: "How to complete your Out-of-Network Claim Form",
    description: "azblue.com: 8-page PDF" },
  { slug: "az-cms-notice-and-consent-form-10780", source: "azblue/site/cms-notice-and-consent-form-10780.pdf", title: "Standard Notice and Consent Documents Under the No Surprises Act",
    description: "azblue.com: 16-page PDF" },
  { slug: "az-cobra-application", source: "azblue/site/cobra-application.pdf", title: "COBRA employee application",
    description: "azblue.com: 2-page PDF" },
  { slug: "az-commercial-diabetes-management-workbook-update-es", source: "azblue/site/commercial-diabetes-management-workbook-update-es.pdf", title: "Mi diabetes: Cuaderno de trabajo/plan de acción",
    description: "azblue.com: 16-page PDF" },
  { slug: "az-dental-claim-form", source: "azblue/site/dental-claim-form.pdf", title: "Dental Claim Form",
    description: "azblue.com: 2-page PDF" },
  { slug: "az-ms-dental-claim-form", source: "azblue/site/ms-dental-claim-form.pdf", title: "Medicare Supplement Dental Claim Form",
    description: "azblue.com: 2-page PDF" },
  { slug: "az-employee-application-spanish", source: "azblue/site/employee-application-spanish.pdf", title: "Solicitud de empleado",
    description: "azblue.com: 2-page PDF" },
  { slug: "az-geoblue-plans-and-services-flyer", source: "azblue/site/geoblue-plans-and-services-flyer.pdf", title: "Blue Cross Blue Shield Global Health Insurance Plans and Services",
    description: "azblue.com: 2-page PDF" },
  { slug: "az-large-group-member-guide-with-rx", source: "azblue/site/large-group-member-guide-with-rx.pdf", title: "2026 Member Guide",
    description: "azblue.com: 11-page PDF" },
  { slug: "az-ma-silversneakers-benefit-flyer-508-en-sp", source: "azblue/site/ma-silversneakers-benefit-flyer-508-en-sp.pdf", title: "Now’s Your Time to Move With SilverSneakers",
    description: "azblue.com: 4-page PDF" },
  { slug: "az-ma-sob-plus-maricopa-pinal-508-sp", source: "azblue/site/ma-sob-plus-maricopa-pinal-508-sp.pdf", title: "2026 Resumen de beneficios",
    description: "azblue.com: 32-page PDF" },
  { slug: "az-pediatric-nicu-case-management-referral-form", source: "azblue/site/pediatric-nicu-case-management-referral-form.pdf", title: "Health Choice Arizona Pediatric / NICU Case Management Referral Form",
    description: "azblue.com: 1-page PDF" },
  { slug: "az-pharmacy-claim", source: "azblue/site/pharmacy-claim.pdf", title: "Pharmacy reimbursement",
    description: "azblue.com: 1-page PDF" },
  { slug: "az-provider-az-standard-prior-auth-form-meds-dme-devices", source: "azblue/site/provider-az-standard-prior-auth-form-meds-dme-devices.pdf", title: "Prior Authorization Request for Medications, DME, and Medical Devices",
    description: "azblue.com: 3-page PDF" },
  { slug: "az-standardappealpacket", source: "azblue/site/standardappealpacket.pdf", title: "Member Appeal and Grievance Process",
    description: "azblue.com: 14-page PDF" },
  { slug: "az-stigma-free-az-media-kit", source: "azblue/site/stigma-free-az-media-kit.pdf", title: "Media Kit",
    description: "azblue.com: 2-page PDF" },
  { slug: "az-your-rights-and-protections-against-surprise-medical-bills", source: "azblue/site/your-rights-and-protections-against-surprise-medical-bills.pdf", title: "Your Rights and Protections Against Surprise Medical Bills",
    description: "azblue.com: 2-page PDF" },
  { slug: "az-maternal-risk-assessment", source: "azblue/site/maternal-risk-assessment.pdf", title: "Maternal Health Risk Assessment Form",
    description: "azblue.com: 2-page form" },
  { slug: "az-remittance-advice-medical", source: "azblue/site/remittance-advice-medical.pdf", title: "Medical remittance advice",
    description: "azblue.com: 1-page remittance advice" },
];

export const SAMPLE_DOCS: SampleDoc[] = ENTRIES.map((d) => ({ ...d, collection: collectionOf(d.source) }));

/** One { slug } per sample document: the generateStaticParams of every /…/[slug] page. */
export const slugParams = (): { slug: string }[] => SAMPLE_DOCS.map((d) => ({ slug: d.slug }));

const BY_SLUG = new Map(SAMPLE_DOCS.map((d) => [d.slug, d]));

export const sampleDoc = (slug: string): SampleDoc | undefined => BY_SLUG.get(slug);
