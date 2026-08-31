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
CREATE VIEW "grundstueck_gueltig" AS
    SELECT
        "grundstueck"."nbident" AS "nbident",
        "grundstueck"."nummer" AS "nummer",
        "grundstueck"."egrid" AS "egrid"
    FROM "grundstueck" "grundstueck"
    WHERE ("grundstueck"."entstehung" IS NOT NULL)
      AND (EXISTS (SELECT 1 FROM "gsnachfuehrung" "v1" WHERE "v1"."id" = "grundstueck"."entstehung" AND "v1"."grundbucheintrag" IS NOT NULL))
      AND ((NOT ("grundstueck"."untergang" IS NOT NULL)) OR (NOT (EXISTS (SELECT 1 FROM "gsnachfuehrung" "v2" WHERE "v2"."id" = "grundstueck"."untergang" AND "v2"."grundbucheintrag" IS NOT NULL))));
CREATE OR REPLACE FUNCTION "uq_grundstueck_gueltig_ch041101_check"() RETURNS trigger AS $$
BEGIN
    IF ((NEW."nbident" IS NOT NULL AND NEW."nummer" IS NOT NULL) AND (NEW."entstehung" IS NOT NULL) AND (EXISTS (SELECT 1 FROM "gsnachfuehrung" "v1" WHERE "v1"."id" = NEW."entstehung" AND "v1"."grundbucheintrag" IS NOT NULL)) AND ((NOT (NEW."untergang" IS NOT NULL)) OR (NOT (EXISTS (SELECT 1 FROM "gsnachfuehrung" "v2" WHERE "v2"."id" = NEW."untergang" AND "v2"."grundbucheintrag" IS NOT NULL))))) AND EXISTS (SELECT 1 FROM "grundstueck" "grundstueck" WHERE "grundstueck"."id" <> NEW."id" AND "grundstueck"."nbident" = NEW."nbident" AND "grundstueck"."nummer" = NEW."nummer" AND ("grundstueck"."entstehung" IS NOT NULL) AND (EXISTS (SELECT 1 FROM "gsnachfuehrung" "v1" WHERE "v1"."id" = "grundstueck"."entstehung" AND "v1"."grundbucheintrag" IS NOT NULL)) AND ((NOT ("grundstueck"."untergang" IS NOT NULL)) OR (NOT (EXISTS (SELECT 1 FROM "gsnachfuehrung" "v2" WHERE "v2"."id" = "grundstueck"."untergang" AND "v2"."grundbucheintrag" IS NOT NULL))))) THEN
        RAISE EXCEPTION 'view "grundstueck_gueltig": UNIQUE CH041101 (nbident, nummer) violated';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER "uq_grundstueck_gueltig_ch041101_trg" BEFORE INSERT OR UPDATE ON "grundstueck"
    FOR EACH ROW EXECUTE FUNCTION "uq_grundstueck_gueltig_ch041101_check"();
CREATE OR REPLACE FUNCTION "uq_grundstueck_gueltig_ch041102_check"() RETURNS trigger AS $$
BEGIN
    IF ((NEW."egrid" IS NOT NULL) AND (NEW."entstehung" IS NOT NULL) AND (EXISTS (SELECT 1 FROM "gsnachfuehrung" "v1" WHERE "v1"."id" = NEW."entstehung" AND "v1"."grundbucheintrag" IS NOT NULL)) AND ((NOT (NEW."untergang" IS NOT NULL)) OR (NOT (EXISTS (SELECT 1 FROM "gsnachfuehrung" "v2" WHERE "v2"."id" = NEW."untergang" AND "v2"."grundbucheintrag" IS NOT NULL))))) AND EXISTS (SELECT 1 FROM "grundstueck" "grundstueck" WHERE "grundstueck"."id" <> NEW."id" AND "grundstueck"."egrid" = NEW."egrid" AND ("grundstueck"."entstehung" IS NOT NULL) AND (EXISTS (SELECT 1 FROM "gsnachfuehrung" "v1" WHERE "v1"."id" = "grundstueck"."entstehung" AND "v1"."grundbucheintrag" IS NOT NULL)) AND ((NOT ("grundstueck"."untergang" IS NOT NULL)) OR (NOT (EXISTS (SELECT 1 FROM "gsnachfuehrung" "v2" WHERE "v2"."id" = "grundstueck"."untergang" AND "v2"."grundbucheintrag" IS NOT NULL))))) THEN
        RAISE EXCEPTION 'view "grundstueck_gueltig": UNIQUE CH041102 (egrid) violated';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER "uq_grundstueck_gueltig_ch041102_trg" BEFORE INSERT OR UPDATE ON "grundstueck"
    FOR EACH ROW EXECUTE FUNCTION "uq_grundstueck_gueltig_ch041102_check"();
