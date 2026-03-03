-- Add columns for currently applied risk profile (allergy/chronic disease).
ALTER TABLE public.user_profile
ADD COLUMN IF NOT EXISTS applied_allergies TEXT,
ADD COLUMN IF NOT EXISTS applied_chronic_diseases TEXT,
ADD COLUMN IF NOT EXISTS food_allergy_detail TEXT;

-- Backfill existing rows so applied fields are immediately usable.
UPDATE public.user_profile
SET
    applied_allergies = COALESCE(applied_allergies, allergies),
    applied_chronic_diseases = COALESCE(applied_chronic_diseases, chronic_diseases),
    food_allergy_detail = COALESCE(
        food_allergy_detail,
        NULLIF(
            TRIM(
                SUBSTRING(allergies FROM '상세정보\\s*:\\s*([^|,]+(?:\\s*[,/;]\\s*[^|,]+)*)')
            ),
            ''
        )
    );
