CREATE TABLE "gsnachfuehrung" (
    "id" text UNIQUE NOT NULL,
    "nbident" varchar(12) NOT NULL,
    "identifikator" varchar(12) NOT NULL,
    "gueltigereintrag" varchar(10) NOT NULL,
    "grundbucheintrag" varchar(10),
    CONSTRAINT uq_gsnachfuehrung_nbident_identifikator UNIQUE ("nbident", "identifikator")
);
CREATE TABLE "grundstueck" (
    "id" text UNIQUE NOT NULL,
    "nbident" varchar(12) NOT NULL,
    "nummer" varchar(12) NOT NULL,
    "egrid" varchar(14),
    "entstehung" text,
    "untergang" text
);
ALTER TABLE "grundstueck" ADD CONSTRAINT fk_grundstueck_entstehung FOREIGN KEY ("entstehung") REFERENCES "gsnachfuehrung" ("id");
ALTER TABLE "grundstueck" ADD CONSTRAINT fk_grundstueck_untergang FOREIGN KEY ("untergang") REFERENCES "gsnachfuehrung" ("id");
-- NOTE (view grundstueck_gueltig): [SQL-VIEW-CONSTRAINT-DROPPED] VIEW-level UNIQUE 'CH041101' (NBIdent, Nummer) - a CREATE VIEW cannot enforce it
-- NOTE (view grundstueck_gueltig): [SQL-VIEW-CONSTRAINT-DROPPED] VIEW-level UNIQUE 'CH041102' (EGRID) - a CREATE VIEW cannot enforce it
CREATE VIEW "grundstueck_gueltig" AS
    SELECT
        "grundstueck"."nbident" AS "nbident",
        "grundstueck"."nummer" AS "nummer",
        "grundstueck"."egrid" AS "egrid"
    FROM "grundstueck" "grundstueck"
    WHERE ("grundstueck"."entstehung" IS NOT NULL)
      AND (EXISTS (SELECT 1 FROM "gsnachfuehrung" "v1" WHERE "v1"."id" = "grundstueck"."entstehung" AND "v1"."grundbucheintrag" IS NOT NULL))
      AND ((NOT ("grundstueck"."untergang" IS NOT NULL)) OR (NOT (EXISTS (SELECT 1 FROM "gsnachfuehrung" "v2" WHERE "v2"."id" = "grundstueck"."untergang" AND "v2"."grundbucheintrag" IS NOT NULL))));
