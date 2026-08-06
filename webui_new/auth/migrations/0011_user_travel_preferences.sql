CREATE TABLE IF NOT EXISTS user_travel_preferences (
    user_id TEXT PRIMARY KEY,
    home_location TEXT,
    transportation_preference TEXT,
    hotel_brands JSONB NOT NULL DEFAULT '[]'::jsonb,
    airlines JSONB NOT NULL DEFAULT '[]'::jsonb,
    seat_preference TEXT,
    meal_preference TEXT,
    budget_level TEXT,
    extra_preferences JSONB NOT NULL DEFAULT '{}'::jsonb,
    preference_updated_at JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_user_travel_preferences_hotel_brands_array
        CHECK (jsonb_typeof(hotel_brands) = 'array'),
    CONSTRAINT ck_user_travel_preferences_airlines_array
        CHECK (jsonb_typeof(airlines) = 'array'),
    CONSTRAINT ck_user_travel_preferences_extra_object
        CHECK (jsonb_typeof(extra_preferences) = 'object'),
    CONSTRAINT ck_user_travel_preferences_timestamps_object
        CHECK (jsonb_typeof(preference_updated_at) = 'object')
);

WITH aggregated AS (
    SELECT
        user_id,
        jsonb_object_agg(pref_type, pref_value) AS preferences,
        COALESCE(
            jsonb_object_agg(pref_type, to_jsonb(updated_at)),
            '{}'::jsonb
        ) AS preference_updated_at,
        MIN(updated_at) AS created_at,
        MAX(updated_at) AS updated_at
    FROM user_preferences
    GROUP BY user_id
)
INSERT INTO user_travel_preferences (
    user_id,
    home_location,
    transportation_preference,
    hotel_brands,
    airlines,
    seat_preference,
    meal_preference,
    budget_level,
    extra_preferences,
    preference_updated_at,
    created_at,
    updated_at
)
SELECT
    user_id,
    preferences ->> 'home_location',
    preferences ->> 'transportation_preference',
    CASE
        WHEN preferences -> 'hotel_brands' IS NULL THEN '[]'::jsonb
        WHEN jsonb_typeof(preferences -> 'hotel_brands') = 'array'
            THEN preferences -> 'hotel_brands'
        ELSE jsonb_build_array(preferences -> 'hotel_brands')
    END,
    CASE
        WHEN preferences -> 'airlines' IS NULL THEN '[]'::jsonb
        WHEN jsonb_typeof(preferences -> 'airlines') = 'array'
            THEN preferences -> 'airlines'
        ELSE jsonb_build_array(preferences -> 'airlines')
    END,
    preferences ->> 'seat_preference',
    preferences ->> 'meal_preference',
    preferences ->> 'budget_level',
    preferences - ARRAY[
        'home_location', 'transportation_preference', 'hotel_brands',
        'airlines', 'seat_preference', 'meal_preference', 'budget_level'
    ],
    preference_updated_at,
    created_at,
    updated_at
FROM aggregated
ON CONFLICT (user_id) DO NOTHING;

COMMENT ON TABLE user_preferences IS
    'Deprecated EAV compatibility mirror. Runtime reads user_travel_preferences.';
COMMENT ON TABLE user_travel_preferences IS
    'One row per user: typed core business-travel preferences plus JSONB extensions.';
