# Idempotent Discourse bootstrap, run with `rails runner` inside the official image (profile `full`).
# Creates/updates an admin user, a test category and one all-users admin API key, then prints ONE line
# "IBX_DISCOURSE_JSON <json>" that deploy/bootstrap/discourse_bootstrap.sh writes to the shared volume.
# The API key value is only known at creation time, so an existing key is reused only when the caller passes it
# back (IBX_DISCOURSE_EXISTING_KEY) and it is still valid; otherwise a new key is created and old ibx keys revoked.
require "json"

username = ENV.fetch("IBX_DISCOURSE_ADMIN_USERNAME", "ibx_admin")
email = ENV.fetch("IBX_DISCOURSE_ADMIN_EMAIL", "admin@discourse.ibx.invalid")
password = ENV.fetch("IBX_DISCOURSE_ADMIN_PASSWORD")
key_description = "ibx-local-connector"
category_slug = ENV.fetch("IBX_DISCOURSE_CATEGORY_SLUG", "ibx-test")

# Local test-instance settings: no outgoing email, no setup wizard.
SiteSetting.disable_emails = "yes"
SiteSetting.wizard_enabled = false if SiteSetting.respond_to?(:wizard_enabled=)
SiteSetting.title = "IBX local Discourse"

user = User.find_by_username(username)
if user.nil?
  user = User.new(username: username, email: email, password: password, active: true, approved: true)
  user.save!
end
user.update!(active: true, approved: true) unless user.active && user.approved
user.grant_admin! unless user.admin
user.change_trust_level!(TrustLevel[4]) if user.trust_level < TrustLevel[4]
user.email_tokens.update_all(confirmed: true)

category = Category.find_by(slug: category_slug)
if category.nil?
  category = Category.create!(name: "IBX test", slug: category_slug, user: Discourse.system_user,
                              description: "Local test category (IncuBrix)")
end

existing = ENV["IBX_DISCOURSE_EXISTING_KEY"].to_s
api_key = nil
reused = false
unless existing.empty?
  found = ApiKey.active.find_by(key_hash: ApiKey.hash_key(existing))
  if found
    api_key = existing
    reused = true
  end
end
if api_key.nil?
  ApiKey.where(description: key_description, revoked_at: nil).each { |k| k.update!(revoked_at: Time.zone.now) }
  created = ApiKey.create!(description: key_description, created_by: Discourse.system_user)
  api_key = created.key
end

puts "IBX_DISCOURSE_JSON " + JSON.generate(
  platform: "discourse",
  version: Discourse::VERSION::STRING,
  base_url: ENV.fetch("IBX_DISCOURSE_URL", "http://discourse"),
  api_key: api_key,
  api_username: username,
  admin_username: username,
  category_id: category.id,
  category_slug: category.slug,
  key_reused: reused,
  min_topic_title_length: SiteSetting.min_topic_title_length,
  min_first_post_length: SiteSetting.min_first_post_length,
  duplicate_topic_titles: (SiteSetting.respond_to?(:duplicate_topic_titles) ? SiteSetting.duplicate_topic_titles : SiteSetting.allow_duplicate_topic_titles),
  written_at: Time.now.utc.iso8601,
)
