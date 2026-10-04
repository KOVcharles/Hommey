package com.hommey.backend.common;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.UUID;

public final class StableIds {
  private static final UUID URL_NAMESPACE = UUID.fromString("6ba7b811-9dad-11d1-80b4-00c04fd430c8");

  private StableIds() {}

  public static UUID request(String user, String value) {
    try {
      return UUID.fromString(value);
    } catch (IllegalArgumentException ignored) {
      return uuid5("hommey:request:" + user + ":" + value);
    }
  }

  public static UUID turn(String user, UUID request) {
    return uuid5("hommey:turn:" + user + ":" + request);
  }

  private static UUID uuid5(String name) {
    try {
      var namespace =
          ByteBuffer.allocate(16)
              .putLong(URL_NAMESPACE.getMostSignificantBits())
              .putLong(URL_NAMESPACE.getLeastSignificantBits())
              .array();
      var sha = MessageDigest.getInstance("SHA-1");
      sha.update(namespace);
      var bytes = sha.digest(name.getBytes(StandardCharsets.UTF_8));
      bytes[6] = (byte) ((bytes[6] & 0x0f) | 0x50);
      bytes[8] = (byte) ((bytes[8] & 0x3f) | 0x80);
      var b = ByteBuffer.wrap(bytes);
      return new UUID(b.getLong(), b.getLong());
    } catch (Exception ex) {
      throw new IllegalStateException(ex);
    }
  }
}
