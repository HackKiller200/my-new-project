terraform {
	required_providers {
		local =  {
			source = "hashicorp/local"
			version = "2.5.0"
			}
	}
}
resource "local_file" "example" {
	filename = var.filename
	content = var.content
}
