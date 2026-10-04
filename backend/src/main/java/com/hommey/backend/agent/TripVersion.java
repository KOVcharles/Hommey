package com.hommey.backend.agent;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.hommey.backend.auth.RegistrationService;
import java.util.*;
import org.springframework.stereotype.Component;

@Component
public class TripVersion {
  private final ObjectMapper mapper;

  public TripVersion(ObjectMapper mapper) {
    this.mapper = mapper;
  }

  public long version(Map<String, Object> trip) {
    var copy = new LinkedHashMap<>(trip);
    copy.remove("updated_at");
    return Long.parseLong(hash(copy).substring(0, 12), 16);
  }

  public String hash(Object value) {
    return RegistrationService.digest(canonical(mapper.valueToTree(value)));
  }

  private String canonical(JsonNode node) {
    if (node.isObject()) {
      var keys = new ArrayList<String>();
      node.fieldNames().forEachRemaining(keys::add);
      Collections.sort(keys);
      return "{"
          + String.join(
              ", ",
              keys.stream()
                  .map(k -> canonical(mapper.valueToTree(k)) + ": " + canonical(node.get(k)))
                  .toList())
          + "}";
    }
    if (node.isArray()) {
      var values = new ArrayList<String>();
      node.forEach(v -> values.add(canonical(v)));
      return "[" + String.join(", ", values) + "]";
    }
    return node.toString();
  }
}
