package com.hommey.backend.profile;

import com.hommey.backend.common.*;
import java.util.*;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

@Repository
public class ProfileRepository {
  private final JdbcClient jdbc;
  private final JsonCodec json;

  public ProfileRepository(JdbcClient jdbc, JsonCodec json) {
    this.jdbc = jdbc;
    this.json = json;
  }

  public Map<String, Object> get(String user) {
    var rows =
        jdbc.sql(
                "SELECT profile,onboarding_status,revision,updated_at FROM user_personal_profiles WHERE user_id=:user")
            .param("user", user)
            .query()
            .listOfRows();
    if (rows.isEmpty()) {
      var result = new LinkedHashMap<String, Object>();
      result.put("profile", PersonalProfile.defaults());
      result.put("onboarding_status", "pending");
      result.put("revision", 0);
      result.put("updated_at", null);
      return result;
    }
    return decode(rows.getFirst());
  }

  public Map<String, Object> write(
      String user, PersonalProfile profile, String status, int revision) {
    String payload = profile == null ? null : json.write(profile.withProjectIds());
    var rows =
        revision == 0
            ? jdbc.sql(
                    """
            INSERT INTO user_personal_profiles(user_id,profile,onboarding_status,revision)
            VALUES(:user,CAST(:profile AS jsonb),:status,1) ON CONFLICT(user_id) DO NOTHING
            RETURNING profile,onboarding_status,revision,updated_at
            """)
                .param("user", user)
                .param(
                    "profile", payload == null ? json.write(PersonalProfile.defaults()) : payload)
                .param("status", status)
                .query()
                .listOfRows()
            : jdbc.sql(
                    """
            UPDATE user_personal_profiles SET profile=COALESCE(CAST(:profile AS jsonb),profile),
              onboarding_status=:status,revision=revision+1,updated_at=NOW()
            WHERE user_id=:user AND revision=:revision RETURNING profile,onboarding_status,revision,updated_at
            """)
                .param("user", user)
                .param("profile", payload)
                .param("status", status)
                .param("revision", revision)
                .query()
                .listOfRows();
    if (rows.isEmpty()) throw new ApiException(409, "PROFILE_CONFLICT", "资料已更新，请重新打开后编辑");
    return decode(rows.getFirst());
  }

  private Map<String, Object> decode(Map<String, Object> row) {
    var result = new LinkedHashMap<>(row);
    result.put("profile", json.read(row.get("profile")));
    result.put("updated_at", row.get("updated_at").toString());
    return result;
  }
}
