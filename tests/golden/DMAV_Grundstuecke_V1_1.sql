CREATE TABLE "liegenschaft" (
    "id" text UNIQUE NOT NULL,
    "nummerteilgrundstueck" varchar(12),
    "fiktiv" boolean NOT NULL,
    "flaechenmass" integer NOT NULL,
    "qualitaetsstandard" text NOT NULL,
    "grundstueck" text
);
-- NOTE (liegenschaft): [SQL-GEOM-NO-CRS] Geometrie: vertex CoordType not resolved - provide the geometry base model via --repo
-- NOTE (liegenschaft): [BUILD-TYPE-UNRESOLVED] Streitig: attribute type not resolved by the model builder - provide the imported model via --repo
-- NOTE (liegenschaft): [SQL-CHECK-EXPR-UNSUPPORTED] MANDATORY CONSTRAINT 'CH041201': DEFINED(streitig): no such column - CHECK not generated
CREATE TABLE "selbstaendigesdauerndesrecht" (
    "id" text UNIQUE NOT NULL,
    "nummerteilgrundstueck" varchar(12),
    "flaechenmass" integer NOT NULL,
    "istbaurecht" boolean,
    "grundstueck" text
);
-- NOTE (selbstaendigesdauerndesrecht): [SQL-GEOM-NO-CRS] Geometrie: vertex CoordType not resolved - provide the geometry base model via --repo
-- NOTE (selbstaendigesdauerndesrecht): [BUILD-TYPE-UNRESOLVED] Streitig: attribute type not resolved by the model builder - provide the imported model via --repo
-- NOTE (selbstaendigesdauerndesrecht): [SQL-CHECK-EXPR-UNSUPPORTED] MANDATORY CONSTRAINT 'CH041601': DEFINED(streitig): no such column - CHECK not generated
CREATE TABLE "bergwerk" (
    "id" text UNIQUE NOT NULL,
    "nummerteilgrundstueck" varchar(12),
    "flaechenmass" integer NOT NULL,
    "grundstueck" text
);
-- NOTE (bergwerk): [SQL-GEOM-NO-CRS] Geometrie: vertex CoordType not resolved - provide the geometry base model via --repo
-- NOTE (bergwerk): [BUILD-TYPE-UNRESOLVED] Streitig: attribute type not resolved by the model builder - provide the imported model via --repo
-- NOTE (bergwerk): [SQL-CHECK-EXPR-UNSUPPORTED] MANDATORY CONSTRAINT 'CH042001': DEFINED(streitig): no such column - CHECK not generated
CREATE TABLE "gsnachfuehrung" (
    "id" text UNIQUE NOT NULL,
    "nbident" varchar(12) NOT NULL,
    "identifikator" varchar(12) NOT NULL,
    "beschreibung" varchar(60) NOT NULL,
    "mutationsart" text NOT NULL,
    "gueltigereintrag" timestamp NOT NULL,
    "grundbucheintrag" timestamp,
    CONSTRAINT uq_gsnachfuehrung_nbident_identifikator UNIQUE ("nbident", "identifikator")
);
-- NOTE (gsnachfuehrung): [SQL-GEOM-NO-CRS] Perimeter: vertex CoordType not resolved - provide the geometry base model via --repo
CREATE TABLE "grenzpunkt" (
    "id" text UNIQUE NOT NULL,
    "nbident" varchar(12),
    "nummer" varchar(12),
    "hoehengeometrie" numeric,
    "lagegenauigkeit" numeric NOT NULL,
    "istlagezuverlaessig" boolean NOT NULL,
    "hoehengenauigkeit" numeric,
    "isthoehenzuverlaessig" boolean,
    "punktzeichen" text NOT NULL,
    "isthoheitsgrenzpunkt" boolean NOT NULL,
    "isthoheitsgrenzsteinalt" boolean NOT NULL,
    "istexaktdefiniert" boolean NOT NULL,
    "symbolori" numeric,
    "entstehung" text,
    "untergang" text,
    CONSTRAINT chk_grenzpunkt_ch040201 CHECK ((("hoehengeometrie" IS NOT NULL) = ("hoehengenauigkeit" IS NOT NULL))),
    CONSTRAINT chk_grenzpunkt_ch040202 CHECK ((("hoehengeometrie" IS NOT NULL) = ("isthoehenzuverlaessig" IS NOT NULL))),
    CONSTRAINT chk_grenzpunkt_ch040203 CHECK (("istexaktdefiniert" OR ("punktzeichen" = 'unversichert')))
);
-- NOTE (grenzpunkt): [BUILD-TYPE-UNRESOLVED] Geometrie: attribute type not resolved by the model builder - provide the imported model via --repo
CREATE TABLE "grundstueck" (
    "id" text UNIQUE NOT NULL,
    "nbident" varchar(12) NOT NULL,
    "nummer" varchar(12) NOT NULL,
    "egrid" varchar(14),
    "iststreitig" boolean NOT NULL,
    "istvollstaendig" boolean NOT NULL,
    "grundstuecksart" text NOT NULL,
    "fiktiv" boolean NOT NULL,
    "gesamtflaechenmass" integer,
    "entstehung" text,
    "untergang" text,
    CONSTRAINT chk_grundstueck_ch040701 CHECK (("istvollstaendig" = (NOT ("gesamtflaechenmass" IS NOT NULL))))
);
CREATE TABLE "grundstueck_textposition" (
    "id" text UNIQUE NOT NULL,
    "grundstueck_fk" text NOT NULL,
    "orientierung" numeric,
    "darstellungin" text,
    "textgroesse" text,
    "hreferenzpunkt" text,
    "vreferenzpunkt" text
);
-- NOTE (grundstueck_textposition): [BUILD-TYPE-UNRESOLVED] Position: attribute type not resolved by the model builder - provide the imported model via --repo
-- NOTE (grundstueck_textposition): [BUILD-TYPE-UNRESOLVED] Hinweisstrich: attribute type not resolved by the model builder - provide the imported model via --repo
ALTER TABLE "liegenschaft" ADD CONSTRAINT fk_liegenschaft_grundstueck FOREIGN KEY ("grundstueck") REFERENCES "grundstueck" ("id");
ALTER TABLE "selbstaendigesdauerndesrecht" ADD CONSTRAINT fk_selbstaendigesdauerndesrecht_grundstueck FOREIGN KEY ("grundstueck") REFERENCES "grundstueck" ("id");
ALTER TABLE "bergwerk" ADD CONSTRAINT fk_bergwerk_grundstueck FOREIGN KEY ("grundstueck") REFERENCES "grundstueck" ("id");
ALTER TABLE "grenzpunkt" ADD CONSTRAINT fk_grenzpunkt_entstehung FOREIGN KEY ("entstehung") REFERENCES "gsnachfuehrung" ("id");
ALTER TABLE "grenzpunkt" ADD CONSTRAINT fk_grenzpunkt_untergang FOREIGN KEY ("untergang") REFERENCES "gsnachfuehrung" ("id");
ALTER TABLE "grundstueck" ADD CONSTRAINT fk_grundstueck_entstehung FOREIGN KEY ("entstehung") REFERENCES "gsnachfuehrung" ("id");
ALTER TABLE "grundstueck" ADD CONSTRAINT fk_grundstueck_untergang FOREIGN KEY ("untergang") REFERENCES "gsnachfuehrung" ("id");
ALTER TABLE "grundstueck_textposition" ADD CONSTRAINT fk_grundstueck_textposition_grundstueck_fk FOREIGN KEY ("grundstueck_fk") REFERENCES "grundstueck" ("id");
-- NOTE (view grenzpunkt_gueltig): [SQL-VIEW-ATTR-DROPPED] attribute 'Geometrie' not in the CREATE VIEW: 'Geometrie' has no mapped column on table 'grenzpunkt'
-- NOTE (view grenzpunkt_gueltig): [SQL-VIEW-CONSTRAINT-DROPPED] VIEW-level UNIQUE 'CH040601' (Geometrie) - a CREATE VIEW cannot enforce it
CREATE VIEW "grenzpunkt_gueltig" AS
    SELECT
        "grenzpunkt"."nbident" AS "nbident",
        "grenzpunkt"."nummer" AS "nummer",
        "grenzpunkt"."hoehengeometrie" AS "hoehengeometrie",
        "grenzpunkt"."lagegenauigkeit" AS "lagegenauigkeit",
        "grenzpunkt"."istlagezuverlaessig" AS "istlagezuverlaessig",
        "grenzpunkt"."hoehengenauigkeit" AS "hoehengenauigkeit",
        "grenzpunkt"."isthoehenzuverlaessig" AS "isthoehenzuverlaessig",
        "grenzpunkt"."punktzeichen" AS "punktzeichen",
        "grenzpunkt"."isthoheitsgrenzpunkt" AS "isthoheitsgrenzpunkt",
        "grenzpunkt"."isthoheitsgrenzsteinalt" AS "isthoheitsgrenzsteinalt",
        "grenzpunkt"."istexaktdefiniert" AS "istexaktdefiniert",
        "grenzpunkt"."symbolori" AS "symbolori"
    FROM "grenzpunkt" "grenzpunkt"
    WHERE ("grenzpunkt"."entstehung" IS NOT NULL)
      AND (EXISTS (SELECT 1 FROM "gsnachfuehrung" "v1" WHERE "v1"."id" = "grenzpunkt"."entstehung" AND "v1"."grundbucheintrag" IS NOT NULL))
      AND ((NOT ("grenzpunkt"."untergang" IS NOT NULL)) OR (NOT (EXISTS (SELECT 1 FROM "gsnachfuehrung" "v2" WHERE "v2"."id" = "grenzpunkt"."untergang" AND "v2"."grundbucheintrag" IS NOT NULL))));
-- NOTE (view grundstueck_gueltig): [SQL-VIEW-ATTR-DROPPED] attribute 'Textposition' not in the CREATE VIEW: 'Textposition' has no mapped column on table 'grundstueck'
-- NOTE (view grundstueck_gueltig): [SQL-VIEW-CONSTRAINT-DROPPED] VIEW-level UNIQUE 'CH041101' (NBIdent, Nummer) - a CREATE VIEW cannot enforce it
-- NOTE (view grundstueck_gueltig): [SQL-VIEW-CONSTRAINT-DROPPED] VIEW-level UNIQUE 'CH041102' (EGRID) - a CREATE VIEW cannot enforce it
CREATE VIEW "grundstueck_gueltig" AS
    SELECT
        "grundstueck"."nbident" AS "nbident",
        "grundstueck"."nummer" AS "nummer",
        "grundstueck"."egrid" AS "egrid",
        "grundstueck"."iststreitig" AS "iststreitig",
        "grundstueck"."istvollstaendig" AS "istvollstaendig",
        "grundstueck"."grundstuecksart" AS "grundstuecksart",
        "grundstueck"."fiktiv" AS "fiktiv",
        "grundstueck"."gesamtflaechenmass" AS "gesamtflaechenmass"
    FROM "grundstueck" "grundstueck"
    WHERE ("grundstueck"."entstehung" IS NOT NULL)
      AND (EXISTS (SELECT 1 FROM "gsnachfuehrung" "v1" WHERE "v1"."id" = "grundstueck"."entstehung" AND "v1"."grundbucheintrag" IS NOT NULL))
      AND ((NOT ("grundstueck"."untergang" IS NOT NULL)) OR (NOT (EXISTS (SELECT 1 FROM "gsnachfuehrung" "v2" WHERE "v2"."id" = "grundstueck"."untergang" AND "v2"."grundbucheintrag" IS NOT NULL))));
-- NOTE (view liegenschaft_gueltig): [SQL-VIEW-ATTR-DROPPED] attribute 'Geometrie' not in the CREATE VIEW: 'Geometrie' has no mapped column on table 'liegenschaft'
-- NOTE (view liegenschaft_gueltig): [SQL-VIEW-ATTR-DROPPED] attribute 'Streitig' not in the CREATE VIEW: 'Streitig' has no mapped column on table 'liegenschaft'
-- NOTE (view liegenschaft_gueltig): [SQL-VIEW-CONSTRAINT-DROPPED] VIEW-level SetConstraint 'CH041501' - a whole-population check no CREATE VIEW can carry
CREATE VIEW "liegenschaft_gueltig" AS
    SELECT
        "liegenschaft"."nummerteilgrundstueck" AS "nummerteilgrundstueck",
        "liegenschaft"."fiktiv" AS "fiktiv",
        "liegenschaft"."flaechenmass" AS "flaechenmass",
        "liegenschaft"."qualitaetsstandard" AS "qualitaetsstandard"
    FROM "liegenschaft" "liegenschaft"
    WHERE (EXISTS (SELECT 1 FROM "grundstueck" "v1" WHERE "v1"."id" = "liegenschaft"."grundstueck" AND "v1"."entstehung" IS NOT NULL))
      AND (EXISTS (SELECT 1 FROM "grundstueck" "v2" WHERE "v2"."id" = "liegenschaft"."grundstueck" AND EXISTS (SELECT 1 FROM "gsnachfuehrung" "v3" WHERE "v3"."id" = "v2"."entstehung" AND "v3"."grundbucheintrag" IS NOT NULL)))
      AND ((NOT (EXISTS (SELECT 1 FROM "grundstueck" "v4" WHERE "v4"."id" = "liegenschaft"."grundstueck" AND "v4"."untergang" IS NOT NULL))) OR (NOT (EXISTS (SELECT 1 FROM "grundstueck" "v5" WHERE "v5"."id" = "liegenschaft"."grundstueck" AND EXISTS (SELECT 1 FROM "gsnachfuehrung" "v6" WHERE "v6"."id" = "v5"."untergang" AND "v6"."grundbucheintrag" IS NOT NULL)))));
-- NOTE (view selbstaendigesdauerndesrecht_gueltig): [SQL-VIEW-ATTR-DROPPED] attribute 'Geometrie' not in the CREATE VIEW: 'Geometrie' has no mapped column on table 'selbstaendigesdauerndesrecht'
-- NOTE (view selbstaendigesdauerndesrecht_gueltig): [SQL-VIEW-ATTR-DROPPED] attribute 'Streitig' not in the CREATE VIEW: 'Streitig' has no mapped column on table 'selbstaendigesdauerndesrecht'
CREATE VIEW "selbstaendigesdauerndesrecht_gueltig" AS
    SELECT
        "selbstaendigesdauerndesrecht"."nummerteilgrundstueck" AS "nummerteilgrundstueck",
        "selbstaendigesdauerndesrecht"."flaechenmass" AS "flaechenmass",
        "selbstaendigesdauerndesrecht"."istbaurecht" AS "istbaurecht"
    FROM "selbstaendigesdauerndesrecht" "selbstaendigesdauerndesrecht"
    WHERE (EXISTS (SELECT 1 FROM "grundstueck" "v1" WHERE "v1"."id" = "selbstaendigesdauerndesrecht"."grundstueck" AND "v1"."entstehung" IS NOT NULL))
      AND (EXISTS (SELECT 1 FROM "grundstueck" "v2" WHERE "v2"."id" = "selbstaendigesdauerndesrecht"."grundstueck" AND EXISTS (SELECT 1 FROM "gsnachfuehrung" "v3" WHERE "v3"."id" = "v2"."entstehung" AND "v3"."grundbucheintrag" IS NOT NULL)))
      AND ((NOT (EXISTS (SELECT 1 FROM "grundstueck" "v4" WHERE "v4"."id" = "selbstaendigesdauerndesrecht"."grundstueck" AND "v4"."untergang" IS NOT NULL))) OR (NOT (EXISTS (SELECT 1 FROM "grundstueck" "v5" WHERE "v5"."id" = "selbstaendigesdauerndesrecht"."grundstueck" AND EXISTS (SELECT 1 FROM "gsnachfuehrung" "v6" WHERE "v6"."id" = "v5"."untergang" AND "v6"."grundbucheintrag" IS NOT NULL)))));
-- NOTE (view bergwerk_gueltig): [SQL-VIEW-ATTR-DROPPED] attribute 'Geometrie' not in the CREATE VIEW: 'Geometrie' has no mapped column on table 'bergwerk'
-- NOTE (view bergwerk_gueltig): [SQL-VIEW-ATTR-DROPPED] attribute 'Streitig' not in the CREATE VIEW: 'Streitig' has no mapped column on table 'bergwerk'
CREATE VIEW "bergwerk_gueltig" AS
    SELECT
        "bergwerk"."nummerteilgrundstueck" AS "nummerteilgrundstueck",
        "bergwerk"."flaechenmass" AS "flaechenmass"
    FROM "bergwerk" "bergwerk"
    WHERE (EXISTS (SELECT 1 FROM "grundstueck" "v1" WHERE "v1"."id" = "bergwerk"."grundstueck" AND "v1"."entstehung" IS NOT NULL))
      AND (EXISTS (SELECT 1 FROM "grundstueck" "v2" WHERE "v2"."id" = "bergwerk"."grundstueck" AND EXISTS (SELECT 1 FROM "gsnachfuehrung" "v3" WHERE "v3"."id" = "v2"."entstehung" AND "v3"."grundbucheintrag" IS NOT NULL)))
      AND ((NOT (EXISTS (SELECT 1 FROM "grundstueck" "v4" WHERE "v4"."id" = "bergwerk"."grundstueck" AND "v4"."untergang" IS NOT NULL))) OR (NOT (EXISTS (SELECT 1 FROM "grundstueck" "v5" WHERE "v5"."id" = "bergwerk"."grundstueck" AND EXISTS (SELECT 1 FROM "gsnachfuehrung" "v6" WHERE "v6"."id" = "v5"."untergang" AND "v6"."grundbucheintrag" IS NOT NULL)))));
